# Source-aware runner routing

**Date:** 2026-07-27
**Status:** Shipped — except the `ace_web` PRODUCER, which lives in ace-web (`turn_driver`) and is not this repo's change. Until it posts `origin=ace_web`, a rule on that source is inert.
**Builds on:** `2026-07-24-directed-runner-routing-design.md` (RunnerAssignment as the routing authority, the availability cascade, pins, drills)
**Related:** `2026-07-25-cloud-agent-bootstrap-design.md` (the cloud runner this exists to target)

## Problem

An agent's work all routes the same way regardless of where it came from. The
per-agent `RunnerAssignment` list answers "which box runs Echo", but the real
operating question is "which box runs *this kind of* Echo work":

- **ace-web** delegates execution to canopy-web (SP4). That work belongs on the
  cloud runner — it is why the cloud runner exists.
- **Email** turns are enqueued by the runner's inbox watcher
  (`packages/canopy_runner/canopy_runner/inbox.py`) and land **unpinned**, so the
  laptop can discover a thread and the cloud box can claim the reply. The box that
  watches the mailbox and the box that answers it are decoupled by accident, not
  by design.
- **Scheduled** turns fire at times the laptop is likely closed.

Today all three are indistinguishable at claim time, and two of them are not even
distinguishable in the record: chat sends and ace-web POSTs both arrive as
`origin="api"`, the enum's catch-all.

Sessions are the one case already solved — `RunnerBinding` stickiness pins a
conversation's turns to the box hosting it, and placement stays the user's call
when that box goes away. Nothing here changes that.

## Decisions

- **Source IS `Turn.origin`, extended.** One vocabulary across the column, the
  ledger, the fleet turn log and the routing UI. A second `source` field beside
  `origin` was rejected: two overlapping columns every producer must set correctly
  is precisely how `origin` became a catch-all in the first place. `origin_ref`
  keeps the detail (thread id, schedule id, sender).
- **A source rule is one priority runner plus a strict toggle**, not a second
  ordered list. Ordering already exists once, in the default list; a source needs
  to say "prefer this box" and "…and nowhere else", nothing more.
- **Rules live on `RunnerAssignment`**, which stays the single routing authority.
  A new `source` column: `""` means the row is part of the agent's default ordered
  list (today's behaviour, untouched); non-empty means the row is that source's
  priority.
- **Precedence is explicit → sticky → source → default.** An explicitly named
  runner (`api` turn with `runner_id`, a drill) beats a session binding, which
  beats a source rule, which beats the default order.
- **`api` gains an explicit `runner_id`, and that retires the `drill` origin.**
  A drill was only ever "an api turn pinned to a box, with a `RunnerDrill` row" —
  the FK was always its real identity.

## The vocabulary

Six values. `origin` widens `max_length` 10 → 32 on **both** `Turn` and `Item`
(`Item.origin` shares `Turn.ORIGIN_CHOICES`); widening a varchar in Postgres is a
metadata-only change, no table rewrite.

| value | means | producer | caller may POST | offered as a rule |
|---|---|---|---|---|
| `ace_web` | ace-web delegated execution | ace-web `turn_driver` | ✅ | ✅ |
| `canopy_web_chat` | a send in canopy-web's chat UI | `canopy_sessions.services` ×2 | ❌ server-only | ✅ |
| `canopy_scheduler` | a schedule fired — recurring or one-shot, on- or off-cycle | `fire_schedule`, `run_schedule_now` | ❌ server-only | ✅ |
| `email` | inbound mail became work | the runner's inbox watcher | ✅ | ✅ |
| `slack` | reserved for a future inbound Slack producer | none yet | ✅ | ✅ |
| `api` | everything else | external callers, item dispatch, drills | ✅ + `runner_id` | ✅ ("unclassified") |

`runner_id` is accepted on any POSTed turn, not only `api` ones; it is listed there
because that is the bucket that needs it (drills, and any caller that already knows
which box it wants).

Retired: `board` (no producer), `manual` (it is an API call), `cron` (renamed —
the scheduler is not only cron once one-shot schedules exist), `drill` (redundant
with the `RunnerDrill` FK), and the un-prefixed `chat`.

**Existing rows remap:** `cron`→`canopy_scheduler`, `manual`/`drill`/`board`→`api`.

**Server-only values are enforced at the request boundary, not in the parser.**
`schemas.Origin` (the `TurnIn` literal) and the items API view reject
`canopy_web_chat` / `canopy_scheduler`; `TurnSpec.from_dict` stays a pure parser so
server-authored dispatch specs — `_raise_schedule_nag`, whose `implement` re-runs a
schedule — can still carry `canopy_scheduler`. Today `from_dict` takes `origin` as a
free string out of Item JSON and bypasses the `Origin` literal entirely; that hole
closes in the items API view, which is where caller-supplied payloads actually enter.

**Retired spellings normalize rather than 422.** The live fleet posts `cron` and
`manual` today (agents raising Items, external enqueues), so rejecting them would
break Echo and Ada the moment this deploys. The boundary maps `board`/`manual`/`drill`
→ `api` and `cron` → `canopy_scheduler` — the same mapping the data migration applies
to existing rows, shared as one constant so the two cannot disagree. `cron` therefore
reaches a server-only value through its alias; that is a deliberate one-release shim,
marked for removal once the fleet is confirmed clean.

Once routing keys on origin, a caller-supplied origin is a routing input. The blast
radius is bounded by the gates that already exist: you can only enqueue into your own
workspace, and a rule only redirects that agent's work among runners you can see. Not
nothing, but not an escalation — worth stating rather than discovering.

## The rule model

`RunnerAssignment` gains two columns:

```python
source = models.CharField(max_length=32, blank=True, default="")
strict = models.BooleanField(default=False)
```

Constraints — the existing `one_assignment_per_agent_runner` is replaced by two
conditional ones:

- `UniqueConstraint(agent, runner, condition=Q(source=""))` — one default row per
  runner, as today.
- `UniqueConstraint(agent, source, condition=~Q(source=""))` — **one priority
  runner per (agent, source)**.

`rank` is meaningless on a source row (uniqueness makes it a single row); write `0`.
The column stays free for a future ordered source list without another migration.
`enabled` keeps its existing meaning uniformly: a disabled row never routes, so a
disabled source row is simply the rule switched off.

### Claim-time composition

One helper builds the ordered list, and the existing cascade runs over it unchanged:

```python
def assignment_rows_for(agent_id, origin) -> list[(rank, Runner)]:
    p = enabled priority row for (agent_id, origin)
    if p and p.strict:  return [p.runner]                    # nothing else may claim
    if p:               return [p.runner] + defaults - {p.runner}   # dedup, priority keeps its place
    return defaults                                          # today's behaviour
```

Rank order, availability, `enabled`, the 60s wedged-runner grace and drills are all
untouched — `claim_next_turn` only picks *which* list to cascade over. Under a strict
rule the grace cannot leak work sideways, because the other runners are not in the
list at all (`_assignment_allows_for_agent` returns `mine is None` → False).

`claim_next_turn` batch-loads every candidate agent's rows in one query as it does
now, splits them into defaults and priorities, and composes per turn in memory.

**`unclaimable_queued_turns` must call the same helper.** These two disagreeing is
the drift class that already has a parity test in this codebase
(`tests/test_claim_schedule_parity.py`); coverage is now per-`(agent, origin)`, not
per-agent, and a strict rule pointing at an offline box reports `offline`
(recoverable) rather than `config` (never runs).

### What is unchanged

- **Session turns.** `runner_target_q`'s stickiness leg is untouched: a bound
  session's turns match only its binding holder, and an unplaced bound session
  claims nowhere. A `canopy_web_chat` rule therefore only ever decides the **first**
  send of a new, unpinned session — after that the binding owns it.
- **Project turns** have no agent, so no rules; they route by
  `capabilities.projects` as today.
- **The coarse SQL match.** A runner named only in a source rule already matches
  `runner_target_q`'s agent leg (any enabled assignment row), so it becomes
  targetable for that agent and the per-candidate composition decides the rest.
- **Retiring a runner** already cascades its assignment rows, so its source rules
  go with it and those sources revert to the default list.

## API

**`POST /api/harness/turns/`** — `TurnIn` gains `runner_id: UUID | None`, setting
`pinned_runner`. Validated with the same `_runner_visibility_q` predicate the rest
of the harness uses: unknown, retired, or not-yours → 422 "unknown or retired runner
id", never a silent unpinned enqueue. The existing pin guarantees hold — a pin
bypasses assignments and rules, never the tenant gate, never
`one_executing_turn_per_agent`.

**`GET|PUT /api/agents/{slug}/runners`** keeps its shape (the default list) with one
load-bearing fix: **its wholesale delete must be scoped to `source=""`**. As written
it deletes every `RunnerAssignment` row for the agent, so saving the default order
would silently destroy every source rule.

**`GET|PUT /api/agents/{slug}/runner-rules`** — new, same wholesale-replace
discipline, scoped to non-empty-source rows, and gating `runner_id` through the same
`_runner_visibility_q` predicate the default-list PUT uses. A separate endpoint rather than a
combined body so neither write can clobber the other's rows, and so the existing GET
response shape (`list[AgentRunnerOut]`, already consumed by the frontend) does not
break.

```
AgentRunnerRuleOut: source, runner_id, runner_name, kind, strict,
                    online, ready, enabled, queued_count
AgentRunnerRuleIn:  source (RoutableSource literal), runner_id, strict, enabled=True
```

`source` is typed as a `RoutableSource` literal so the generated TypeScript carries
the union and the UI's picker has one source of truth rather than a hardcoded copy.
`queued_count` is queued turns for that `(agent, origin)` — what the parked warning
below renders.

Regenerate `frontend/src/api/generated.ts` (`npm run gen:api`) — the
`regen-openapi.yml` workflow fails the PR otherwise.

## UI — Runners tab (option A: default list, then exceptions)

`RunnerAssignments` keeps today's chip row verbatim, now labelled **Default order**.
Beneath it, an indented **Except when the work comes from** list, one line per rule:

```
▾ Ace
    DEFAULT ORDER
    [1 ● jj-mbp  emdash ↑ ↓ ⏻]  [2 ● cloud-1 cloud ↑ ↓ ⏻]  [+ add]

    EXCEPT WHEN THE WORK COMES FROM
    ace_web  →  [● cloud-1 ▾]   ( only | fall through )   ✕
    [+ rule]
```

- The source picker offers the routable set minus sources already ruled on.
- `only` / `fall through` is the `strict` toggle, and the words state the
  consequence rather than naming the flag.
- `✕` deletes the rule (rules are cheap to re-add; a greyed rule sitting next to a
  greyed runner would read as two kinds of "off").
- **A strict rule whose runner is unavailable says so, with the count:** "⚠ cloud-1
  is offline — 3 ace_web turns are parked, and will stay parked." Strictness parking
  a queue is the toggle working; parking it *silently* is the failure.
- The precedence ladder (explicit runner → session binding → source rule → default
  order) is stated once where rules are edited, so nobody has to infer it.

Mutations reuse the component's existing optimistic-commit machinery — `rowsRef`
against stale closures, `commitSeqRef` against out-of-order responses. Rules are a
second commit lane against the new endpoint, same discipline.

## Migration

1. Widen `Turn.origin` and `Item.origin` to 32; update `ORIGIN_CHOICES`.
2. Data migration: remap `cron`→`canopy_scheduler`, `manual`/`drill`/`board`→`api`
   on both tables.
3. Add `source` + `strict` to `RunnerAssignment`; swap the unique constraint for the
   two conditional ones (no existing row has a non-empty source, so it cannot fail).
4. Producers: `canopy_sessions.services` ×2 → `canopy_web_chat`; `fire_schedule` +
   `run_schedule_now` → `canopy_scheduler`; `start_drill` → `api` (already pinned);
   `_raise_schedule_nag`'s Item and dispatch spec → `canopy_scheduler`.
5. Drop the three `origin == ORIGIN_DRILL` pre-filters (`services.py:153`, `:854`,
   `:904`) and key on the `RunnerDrill` FK, which they already query.
6. Frontend `turnLog.ts`: `originLabel`'s `cron` branch becomes `canopy_scheduler`;
   the `manual` branch keys on `enqueued_by_email` alone; the origin filter's option
   list follows the new vocabulary.

Migrations run before cutover, so old code briefly meets the new schema — a widened
varchar and two added columns are both compatible with it. Remapped origin values are
cosmetic to old code (it reads them as opaque strings) except for the three drill
pre-filters, whose window is one deploy and whose worst case is a drill row left
`pending` until re-drilled.

## Testing

- **Composition**: no rule → default list; rule non-strict → priority first then
  defaults (deduped); rule strict → that runner only; disabled rule → default list.
- **Strictness holds past the grace** — a queued strict turn older than
  `CASCADE_GRACE_SECONDS` still refuses every non-priority runner.
- **Precedence** — a pinned turn ignores a contradicting rule; a bound session
  ignores a rule; a rule beats the default order.
- **Parity** — `unclaimable_queued_turns` and `claim_next_turn` agree per
  `(agent, origin)`, extending the existing parity discipline.
- **The wipe** — `PUT /runners` leaves source rules intact; `PUT /runner-rules`
  leaves the default order intact.
- **Boundary** — a caller POSTing `origin=canopy_scheduler` or
  `canopy_web_chat` is rejected; a server-authored nag dispatch carrying
  `canopy_scheduler` is not.
- **`runner_id`** — pins; 422s on unknown/retired/not-visible; a drill still
  resolves its `RunnerDrill` on fail/cancel now that the origin check is gone.
- **Migration** — a remapped `cron` row reads `canopy_scheduler`; a pre-existing
  drill turn still resolves its drill row.
- End to end, the three real cases: `(ace, ace_web) → cloud-1 strict`,
  `(echo, email) → jj-mbp strict`, `(echo, canopy_scheduler) → cloud-1 fall through`.

## Out of scope

- **One-shot schedules** ("remind me to do monthly goals on date X"). The
  `canopy_scheduler` name is chosen to cover them so nothing renames later, but the
  feature itself — a `run_at` mode on `AgentSchedule`, the runner's firing loop,
  `canopy_cron`, the schedule editor, the MCP tools — touches none of this and gets
  its own spec.
- **A Slack producer.** The value is reserved; nothing produces it.
- **Ordered source lists.** One priority runner per source, deliberately. The `rank`
  column is already there if that changes.
- **Pinning email turns at the producer** (`inbox.py` pinning to itself). Rejected:
  it welds "who watches the mailbox" to "who does the work", so the cloud box could
  not poll while the laptop replies, and it cannot differ per agent when one box
  watches several mailboxes. An explicit pin still wins for anyone who wants it.
