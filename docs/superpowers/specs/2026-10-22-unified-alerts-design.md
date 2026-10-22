# Unified alerts

Status: design, agreed with Jonathan in chat 2026-10-22. Mockups:
https://claude.ai/artifact/9JmxqSpow18UcRNF48vw3y (direction only — the UI is
expected to change once it is live; build it compact).

## Why

Problems in canopy-web surface in a dozen unconnected ways, so they are hard to
find and act on, and most can't be dismissed:

| Today | Where it shows | Act / dismiss |
|---|---|---|
| Tasks, chats, access requests waiting on you | Home (`apps/attention`) | fixed verbs; per-person hide |
| Runner Claude credential dead / capped / on API key | email + push (`harness/credential_notify.py`); runner detail health list | none |
| Runner health checks (e.g. a token "not signed in") | `/supervisor` runner detail only, under the runner's slot name (`subscription-1`), not the label a person gave it ("hal") | none |
| Runner on stale branch / old code | `/supervisor` banner (rules computed in the browser) | retire |
| Turns no runner will claim | `/supervisor` `StuckTurnsAlert` | cancel |
| Agent can't run / lacks its repo | Topology "What needs attention" panel + inline line on agent cards | none |
| Agent setup steps / vault state / missing GitHub token | Workspace Health page, agent Settings | link |
| Vault read anomalies | agent Credentials section + email + push | none |
| Device key waiting, ZK grant waiting | email + push | link |
| Failed turns (`FailureInvestigation`) | nowhere in the UI (Ada reads it over MCP) | Ada only |

Five modules send their own email/push, each with its own "once a day / once per
event" rule. The same problem appears on one screen and not another.

## The model Jonathan agreed

1. **Underlying records stay first-class.** A task is a task, an investigation is
   an investigation, a runner's health is the runner's. They keep their models and
   lifecycles.
2. **An alert is a thin, shared pointer row on top.** One row per problem, never a
   per-screen or per-person copy. It points at its subject and carries what is
   needed to place and act on it.
3. **The source decides** when an alert opens and when it resolves (the thing
   underneath got fixed — nobody has to touch the alert), and which of its errors
   deserve an alert at all (a one-off transient failure can stay an error).
4. **Actions are proposed by the source, per viewer.** Each action has a label in
   the names people gave things, what it does (a fix, a link, a notify), and
   whether taking it **closes** the alert. Notify is an ordinary action; it does
   not close, so the alert stays open and shows who asked whom.
5. **You see an alert only if you have at least one action on it.** No read-only
   banners, no quiet context lines. Someone affected but unable to fix gets a
   notify action (e.g. Sift's owner: "Ask Jonathan to fix it").
6. **Dismiss is one more source-proposed action, and it is shared**: dismissing an
   alert visible to more than one person clears it for everyone, recorded with who
   and when. It returns only if the source raises it again with something new.
7. **Everything is logged and auditable**: raised, action taken (by whom),
   notified (whom), dismissed, auto-resolved, reopened.
8. **Screens don't decide what to show.** Every screen asks one question — the open
   alerts in my scope that I have an action on — and renders them with one shared
   component. Email/push hang off the alert, not off per-module code.

## Home must not become a wasteland

Jonathan's explicit worry: a list he learns to ignore. Rules that guard it, each
enforced in code or a test, not left to source authors' taste:

- **Every alert kind must offer a closing action to someone.** A source whose
  proposed actions for every viewer are all non-closing is a bug; a registry test
  asserts each kind declares at least one closing action.
- **Self-healing.** Reconciled sources (below) resolve an alert the moment their
  condition clears. An alert whose source has not re-confirmed it within its
  kind's `confirm_within` is resolved as stale (logged as such), so a source that
  stops running can't leave rows forever.
- **One alert per problem, not per occurrence.** Identity is `(kind, subject)`;
  repeats bump `occurrences` and `last_seen_at`. Four failed turns from one cause
  are one alert.
- **Raise bar.** Only things a person can resolve. Transient/self-recovering
  conditions (a runner dark for an hour, a capped credential that resets on its
  own) are not alerts — they are facts on their own pages.
- **Shared dismiss sticks** until the source's fingerprint for that subject
  changes (a new fact, not a repeat).
- **Severity orders the list**: `blocking` (work is failing now) above `action`
  (something needs a decision) above `setup` (configuration gap). Home shows a
  count per severity; blocking ones sit at the top.

## Data model (apps/attention — the existing framework app that Home reads)

`Alert` (new, persisted):

- `kind` — registered source kind, e.g. `runner_credential`, `turn_failure`.
- `subject_key` — the source's id for the thing, e.g. `runner:<uuid>:cred:subscription-1`.
  Unique together with `kind`.
- `workspace` (FK, NOT NULL — tenancy invariant) and `scopes` (JSON list of
  strings: `workspace:<slug>`, `runner:<uuid>`, `agent:<slug>`) — what screens it
  belongs on.
- `severity` — `blocking` | `action` | `setup`.
- `title`, `body` — display text written by the source, in people's own names.
- `url` — the subject's own page.
- `fingerprint` — the source's digest of the facts; a change re-opens a dismissed
  alert.
- `status` — `open` | `resolved` | `dismissed`; `resolved_reason` (`source`,
  `action`, `stale`); `closed_by` (user, nullable), `closed_at`.
- `first_seen_at`, `last_seen_at`, `occurrences`, `notified_at`.

`AlertEvent` (new, persisted, never pruned by retention): `alert` FK, `at`,
`kind` (`raised|reconfirmed-changed|acted|notified|dismissed|undismissed|resolved|reopened`),
`actor` (user, nullable for the system), `action_id`, `payload` (JSON: e.g. who
was notified, resolve reason). Every state change and action writes one row in
the same transaction as the change. Its own table rather than `apps/events`
because the event log coalesces rows on `(workspace, source, key)`, prunes after
30 days and is admin-only to read, and none of that suits an audit trail. Each
write ALSO emits a coalescing `apps/events` row (source `alert`) so the fleet's
event log still shows alert activity. `GET /api/me/alerts/{id}/history` returns
an alert's events to anyone the alert is visible to.

**Tenancy.** `Alert.workspace` is the subject's workspace: a runner alert takes
the runner owner's workspace that the subject is attached to (the runner's
enrollment/owner workspace, as routing resolves it), and a turn or agent alert
takes the agent's. An alert is visible to a person only if they are a member of
that workspace's organisation tree AND its source offers them at least one action.
A person outside the organisation never sees it, even if an agent of theirs is
affected. Cross-organisation notify is out of scope for v1.

## Sources

A source is a module in the app that owns the subject (`apps/<app>/alerts.py`),
registered by dotted path like today's attention sources, exposing:

- `KIND`, `SEVERITY`, `CONFIRM_WITHIN` (timedelta or None).
- `reconcile(now) -> list[Raise]` (optional) — for condition-shaped problems: the
  complete set of currently true conditions. The framework upserts each, and
  resolves (`reason=source`) any open alert of this kind not in the set. Run on
  the off-request clock (`apps/common/ticks.py`).
- Or event-driven: the owning code calls `alerts.raise_(...)` / `alerts.resolve(...)`
  where the fact is learned (e.g. the runner event ingest).
- `actions(alert, user) -> list[Action]` — per viewer. `Action`: `id`, `label`,
  `closes: bool`, and either `href` (a link to where the fix is done) or a
  server-side handler `perform(alert, user, action_id)`. Notify actions use the
  framework's `notify(alert, to_users, by=user)`.

Framework, not sources: dismiss bookkeeping, events, notification fan-out,
visibility (`actions` non-empty), staleness, and the read API.

### First sources (v1)

Severities: `runner_credential`, `unclaimable_turns` and `device_key_pending` are
`blocking` (turns fail until someone acts); `turn_failure` is `action`;
`agent_cant_run` is `setup`.

1. `runner_credential` (harness) — a runner Claude credential that is signed out
   (the existing `claude_auth_required` event and the `claude.probe.*` health
   checks). Title uses the **label** the person gave the token (`claude_token_label`
   / login account), never the slot name. Actions: owner/runner admins → "Set a new
   token" (link to the runner's credentials), "Dismiss"; owners of agents routed to
   the runner → "Ask <owner> to fix it" (notify). Resolves when the probe passes.
   Replaces the email in `credential_notify.py` for `claude_auth_required`.
2. `turn_failure` (harness) — one per open/escalated/debugger_failed
   `FailureInvestigation`. Actions: workspace log readers → "Open the investigation",
   "Mark resolved" (closes; same path as `resolve_failure_investigation`).
   Resolves when the investigation resolves.
3. `unclaimable_turns` (harness) — queued turns no runner will claim, one alert per
   agent. Action: "Cancel these turns" (closes), runner/agent admins.
4. `device_key_pending` (harness) — a pending runner device key. Action: "Approve
   key" (link), closes on approval. Replaces `device_key_notify` mail.
5. `agent_cant_run` (agents/harness topology) — a STANDING configuration gap,
   not liveness: an agent that routes to no runner at all, or none of whose
   routed runners declares its repo ("lacks its repo"). A runner that is merely
   offline is not this alert (that's the raise bar; work actually stuck behind
   it surfaces as `unclaimable_turns`). Action: link to the agent's routing
   (closes once routing is fixed, via reconcile).

Device-key invitation and "rekeyed" notices, vault read alerts and ZK grant mail
keep their current code in v1.

Later sources (not v1): vault read anomalies, ZK grant waiting, agent setup gaps,
runner stale code, tasks/chats/access requests (today's attention rows; see below).

## Read API and screens

- `GET /api/me/alerts/` — user-level view (once per organisation, like
  `/api/me/attention/`): open alerts the caller has an action on, each with its
  per-viewer actions. Optional `scope=runner:<id>|agent:<slug>|workspace:<slug>`.
- `POST /api/me/alerts/{id}/act` `{action}` — runs a handler action (server checks
  the action is still offered to this caller), logs it, closes the alert if
  `closes`. Dismiss is an action here.
- Routes declare gates (`@gate`), so MCP gets `list_alerts` / `act_on_alert`.
- `POST /api/me/alerts/{id}/undismiss` — puts a dismissed alert back to open;
  allowed to anyone the source currently offers the dismiss action. Logged.
- `GET /api/me/alerts/?status=dismissed` — dismissed in the last 7 days, for the
  fold.
- **Home**: alerts get their own compact section at the TOP of Home, above
  today's attention list, not mixed into its rows. They don't follow the
  attention list's rules: there's no "quiet" fold (an open alert stays until it's
  resolved or dismissed) and no per-person hide (× is the shared dismiss). Each
  row is one line: severity dot, source chip, title, actions inline, ×. Severity
  count chips cover alerts only. Below them, a collapsed "Dismissed (n)" fold
  shows who dismissed each one and when, with an undo. `AttentionRow` and
  `services.attention` are unchanged.
- **Shared `<AlertList scope=…>` component** used by Home, the Topology side panel,
  the agent page header, and the runner detail in `/supervisor`. Compact by
  default. On Topology it replaces the "Agents whose turns can't run" section, the
  "can't run" badge and the inline "lacks its repo" line. The panel's other two
  lists ("held to some capabilities by another", "runners serving nobody") stay
  as plain facts.
- **Notifications**: on raise (and on re-open), `blocking` alerts email + Web Push
  the people with a *closing* action, once per alert (`notified_at`); `action`
  and `setup` alerts don't push. A notify action sends to its named people and is
  logged.

## Tasks, chats and access requests

They stay first-class and keep their existing attention rows and per-person hide
in v1 — they are already per-person and already on Home. Moving them under
`Alert` (a task ask = an alert whose actions are approve/decline/reply) is a
follow-up once the system sources prove the shape. (Ada's call; flagged below.)

## Non-goals

- No new dashboard page; alerts live on the screens that already exist.
- No change to how tasks, investigations or runner health are stored.
- No per-person alert preferences in v1 beyond today's push settings.

## Calls made by the author (Ada), for review

- The `Alert` model lives in `apps/attention` (the app Home already reads), not a
  new app.
- Tasks/chats/access requests are not migrated under `Alert` in v1.
- v1 sources are the five above; the rest follow once the UI has been lived with.
- Audit is its own never-pruned `AlertEvent` table (mirrored into the event log).
- Severity has three levels; each v1 source's level is set above.
- Visibility stays inside one organisation tree.
- Alerts are their own section at the top of Home, with no quiet fold and no
  per-person hide.
- `agent_cant_run` is configuration only, not liveness.
- Topology keeps its two non-alert lists as facts.
