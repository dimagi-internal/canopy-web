# The event log, and inbound push as its first producer

**Status:** approved 2026-07-30
**Supersedes:** nothing. **Touches:** `apps/harness` (inbox trigger), `runner/canopy_runner`.

## The problem, measured

Mail sent to an agent takes **3–6 minutes** to become a turn. Measured on labs,
2026-07-30, comparing Gmail's own message timestamp against the turn row:

| thread | mail arrived | turn created | delay |
|---|---|---|---|
| eva "Fwd: Connect?" | 17:04:33 | 17:08:21 | 228s |
| echo "PRIDE user story" | 15:06:55 | 15:12:28 | 333s |
| hal "Nova Improvement" | 02:19:55 | 02:23:22 | 207s |

None of that is transport. Enqueue→claim is already fast — the WS wake channel
gives p50 **1.5s** for chat, **0.5s** for scheduled turns, **0.1s** for ace-web.
The delay is one timer: `Config.inbox_poll_seconds = 300`. The runner shells out
to `gog gmail search` every five minutes and that is the only thing that
discovers mail.

The second finding, from the same data: canopy-web has **no durable record of an
operational fault**. `TurnEvent` hangs off a `Turn`, so a fault with no turn has
nowhere to go. `MCPAuditLog` is per-MCP-call. `/timeline` is explicitly a derived
view (`apps/timeline/sources.py`: *"No new tables: every source reads live models
at request time"*), so something must exist durably before it can be shown. The
runner's `failure_log.py` is the right idea — first-then-every-Nth warning on a
repeat-failure streak — but it is an in-memory dict writing to a laptop logfile.
A week of failing stream posts is invisible to the server.

So push health has nowhere to be reported, which is why the alerting question
came before the latency one.

## What we are building

Two apps, both **framework tier**.

1. **`apps/events`** — a durable, fleet-wide log of actions and errors. Not a
   decision queue, not a notification channel. A log.
2. **`apps/inbound`** — the doorbell. Receives a Gmail Pub/Sub push, rings the
   runner that holds the mailbox credentials, and writes to the log.

`events` ships first and stands alone; `inbound` is its first producer.

## Decisions taken, and why

**The log stores actions and errors, and emits nothing.** No `Item`, no push
notification, no timeline event — the same discipline `apps/feedback` follows and
guards with a test. A fault is not a decision: `Item`'s closed set
(`implement`/`skip`/`defer`) does not describe "transcript flush failed 400
times", and `Item` count increases drive Web Push, so a flapping runner would
become a notification storm. Hal sweeps the log on a turn and decides what
deserves action. Promotion-to-`Item` is deliberately **not** built: the rule for
which faults matter should be written after a week of real traffic, not guessed
before the first row exists.

**Repeat faults coalesce onto one row.** `(workspace, source, key)` is the
identity; a repeat bumps `count` and `last_seen_at` rather than inserting. This is
`failure_log`'s streak idea made durable — the interesting signal is "still
broken after 400 attempts", and 400 rows say that worse than one row with a count.

**`Event.workspace` is NOT NULL.** A nullable tenant FK in a read predicate is a
known bug class in this repo: six predicates independently grew a
`workspace_id IS NULL` leg meaning *allow*, and `agents/0013` constrained the
column precisely to end it. Every producer supplies a workspace; a PAT caller
gets its default. There is no NULL-means-visible leg to introduce.

**Gmail push is a doorbell, not a reader.** A Pub/Sub notification carries
`{emailAddress, historyId}` and no content — something must still call Gmail to
learn what changed, and only the runner holds per-agent mailbox credentials
(`gog` OAuth clients). So the receiver resolves mailbox → agent → assigned
runners and sends a `runner.check_inbox` control frame. The runner does the read
it already does. Consequences worth stating:

- No mail credential moves into the web app.
- The read path, the `(thread, messageCount)` idempotency key and the
  "agent's own reply" skip are existing, working code — untouched.
- **A forged doorbell cannot inject a fake email.** The worst a forged ping does
  is cause a `gog` read that finds nothing. Verification still happens, but the
  blast radius is small by construction.
- If no runner is online, nothing is read. That is already true today, so it is
  not a regression — but it is logged at `warn` rather than silently queued.

**The 300s poll stays, and becomes the auditor.** It is no longer the delivery
mechanism, but it is a second independent path to the same discovery, which makes
it a free oracle for push health: *a message the poll discovers is a message push
failed to ring for.* That is a direct observation, not a probe that can itself be
wrong. Every enqueued email turn is tagged `discovered_by: push | poll`, and a
`poll`-discovered turn on a mailbox with a live watch is logged at `error`.

**Failure is loud, never deferred.** A push that arrives for a mailbox with no
online runner, a watch inside 24h of expiry, a watch already expired, a
poll-discovered message — each writes an `error`/`warn` row rather than being
absorbed. "It'll get picked up by the next poll" is the failure mode this spec
exists to make visible, not a defence.

## Architecture

```
Gmail ──watch──▶ Pub/Sub topic ──push──▶ POST /api/inbound/gmail/   (auth=None, OIDC-verified)
                                              │
                                              ├─▶ Event(info, "gmail.push")
                                              │
                                              └─▶ ws/runner/{id}/  {"type": "runner.check_inbox",
                                                                     "mailbox": "eva@dimagi-ai.com"}
                                                        │
runner tick ◀───────────────────────────────────────────┘
   ├─ gog gmail search (that mailbox only, throttle bypassed)
   ├─ POST /api/harness/turns/  origin=email, discovered_by=push
   └─ POST /api/events/         runner-side failure streaks
```

### `apps/events`

`Event` — `workspace` (FK, NOT NULL), `source`, `kind`, `level`, `key`,
`summary`, `payload` (JSON), `count`, `first_seen_at`, `last_seen_at`.

`source` and `kind` are **strings, never FKs** — the same discipline `Item` and
`feedback` follow, and what keeps the app generic over its producers instead of
growing one integration per source. `level` is `info | warn | error`; `info`
carries actions, which is why this is an event log and not an error log.

- `POST /api/events/` — batch ingest, atomic. Coalesces on
  `(workspace, source, key)`. Blank `key` never coalesces (a partial unique index
  excluding blanks, the shape `feedback` already uses for `source_ref`).
- `GET /api/events/` — the pool: `?source=`, `?kind=`, `?level=`, `?since=`,
  cursor-paginated, newest-first.

No mutation route. A log you can edit is not a record. Retention is a management
command (`prune_events --older-than 30d`), not a scheduler — coalescing already
keeps row count near-flat.

`tests/test_events_emits_nothing.py` guards the no-signal rule: no signals
module, no receiver, no `Item` reference, no `AppConfig.ready()` hook.

### `apps/inbound`

`InboundMailbox` — `address` (unique), `agent` (FK), `last_push_at`,
`watch_expires_at`. Explicit data rather than convention: deriving agent `eva`
from `eva@dimagi-ai.com` by string-splitting works right up until it doesn't, and
the failure is silent.

`POST /api/inbound/gmail/` is `auth=None` and self-enforcing, following
`POST /api/auth/token-exchange` exactly (that route is the precedent: `auth=None`,
verifies a header credential itself, rate-limited, explicitly allowlisted in
`apps/common/middleware.py`). Verification is the Google-signed OIDC JWT on the
push request, via `google.oauth2.id_token` — already a dependency, no new
package. An unverified request 404s, never 403: a probe learns nothing about
whether the endpoint exists.

Push-miss detection lives here as a `post_save` receiver on `Turn` (both apps are
framework, so the import is legal): an `origin=email` turn tagged
`discovered_by=poll`, on a mailbox whose `watch_expires_at` is in the future,
writes `Event(level=error, kind="gmail.push.missed")`.

### Runner

- `check_inbox` control frame → mark that mailbox due; the next tick checks it
  immediately, bypassing the per-mailbox 300s stamp. A sixth frame type on the
  channel that already carries `wake`, `interject`, `cancel`, `stream`,
  `menu_answer` — mechanism reused, not invented.
- Tag every email enqueue `discovered_by`.
- Ship `failure_log` streaks to `POST /api/events/`, so laptop faults become
  fleet-visible instead of dying in `~/.canopy/runner.log`.
- **Stop re-reading known threads.** `check_inbox` currently calls
  `newest_sender()` — a `gog gmail thread get` subprocess — for *every* unread
  thread on *every* poll, before the idempotency check, then POSTs an
  `enqueue_turn` the server dedupes anyway. ace and eva sit at 4 unread each, so
  today that is 8 subprocess round-trips every 5 minutes to conclude "already
  tracked". Remember `(thread, messageCount)` locally and skip both calls.

### Gmail watch registration

`users.watch` must be re-armed at least every 7 days. `gog` has no `watch` verb,
so the runner calls the Gmail REST endpoint directly with an access token minted
from the stored refresh token, and re-arms whenever `watch_expires_at` is inside
24h — on the tick it already runs, with no new scheduler (the same reasoning that
kept scheduled turns runner-fired: no celery, no beat, no new deploy surface).

**This is the one part that cannot be verified from this repo.** It needs a GCP
topic, a subscription pointed at the receiver URL, and the
`gmail-api-push@system.gserviceaccount.com` publisher grant. The code ships with
unit tests against a fake transport; the provisioning steps are documented in
`docs/runbooks/gmail-push-provisioning.md` and must be run by a human with GCP
access before push does anything. **Until then the 300s poll is the only path,
which is exactly today's behaviour** — so shipping this is safe with the GCP side
absent, and the log will say so rather than pretending.

## Error handling

Every failure writes a row and none of them silently retries into the void:

| condition | level | kind |
|---|---|---|
| push received, resolved, rung | info | `gmail.push` |
| push for an unknown mailbox | warn | `gmail.push.unknown_mailbox` |
| push received, no online runner assigned | warn | `gmail.push.no_runner` |
| poll found what push should have | error | `gmail.push.missed` |
| watch inside 24h of expiry | warn | `gmail.watch.expiring` |
| watch expired | error | `gmail.watch.expired` |
| runner-side failure streak | error | `runner.<key>.failed` |

## Testing

- `apps/events`: coalescing (repeat bumps count, blank key never coalesces),
  tenancy (a non-member cannot read another workspace's rows), no-signal guard.
- `apps/inbound`: OIDC verification accepts a good token and 404s a forged one;
  unknown mailbox, no-online-runner and push-missed each write the right row;
  the receiver rings every assigned online runner and no others.
- Runner: `check_inbox` frame bypasses the throttle for one mailbox and not the
  others; known threads are not re-read; watch re-arm fires inside the window.
- Architecture: both apps added to `FRAMEWORK` in
  `tests/test_architecture_boundary.py` and to the tier table in
  `ARCHITECTURE.md`.

## Expected result

Mail-triggered turns land in **2–5s** instead of 207–333s, the 300s poll stays as
an auditor that makes push failure loud instead of silent, and every operational
fault in the fleet has somewhere durable to be — with Gmail push as the first of
many producers.
