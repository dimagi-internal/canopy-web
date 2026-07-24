# Directed Runner Routing — assignments, sticky chat, readiness drills

**Date:** 2026-07-24
**Status:** Approved design, pre-implementation
**Supersedes:** the routing half of `2026-07-20-runner-cascade-design.md` Phase B (never shipped); `Agent.runner_preference` (kind-level) as a routing input. Phase A of the cascade spec (the `ready` heartbeat signal) is consumed, not replaced.

## Problem

The fleet now runs three paired runners (two laptop emdash runners on separate macOS
accounts, one EC2 cloud runner), but routing is still shaped for one:

1. **Which runner serves an agent is two-sided and implicit.** A runner's
   `capabilities.agents` says what it pulls; an agent's `runner_preference` ranks
   *kinds*, not runners. Nothing names specific runner instances, so "echo runs on
   THIS laptop, falling back to THAT cloud box" is inexpressible. Standbys must hold
   empty capabilities to stay quiet — which also makes them untestable.
2. **Chat turns ignore the session's home.** Any `sessions:true` runner in the tenant
   can claim any chat turn; the `RunnerBinding` that records which runner holds the
   live emdash session is never consulted at claim time. Resuming a chat can land on
   a runner that has none of that session's live context, while the runner that does
   sits idle.
3. **A standby cannot be verified end-to-end.** There is no way to prove
   "runner R can execute agent X's work" without granting R real capabilities — at
   which point it starts claiming real work and racing the live runner.

The real-world operating loop this design serves: one runner is the working target
until its Claude tokens run out; the operator then shifts the fleet to a second
account or to EC2 — and needs to *know in advance* that both alternates would work.

## Decisions (from design review)

- **One source of truth, on canopy-web.** Per-agent ranked runner assignments,
  stored server-side, consulted by claim routing, chat, drills, and UI alike.
- **Rank = availability cascade.** Lower ranks auto-take-over when higher ranks are
  offline/not-ready (with a grace window); token exhaustion remains a manual reorder
  (an out-of-tokens runner still looks available).
- **Chat failover asks per message.** When a session's bound runner is offline, the
  user chooses "wait" or "continue elsewhere" — never a silent context loss.
- **Drills are full turns** that run each agent's doctor/preflight read-only and
  report an explicit outcome; scoped per-runner (fan out over its assigned agents);
  results persist per (runner, agent) with freshness.
- **v1 excludes**: one-click whole-fleet switch, automatic token-exhaustion
  detection, live-context migration between runners.

## Data model (all in `apps/harness`)

### RunnerAssignment
```
agent      FK -> agents.Agent   (CASCADE)
runner     FK -> harness.Runner (CASCADE)
rank       PositiveSmallInteger  # 0 = first choice
unique (agent, runner)
ordering (agent, rank)
```
The ordered list per agent. Any subset of the fleet; an agent with no rows is
**explicitly unroutable** (surfaced in UI, never silently defaulted). Rows are
replaced wholesale by `PUT /api/agents/{slug}/runners` — the matrix UI saves a full
row, so there is no partial-update ambiguity.

### Turn.pinned_runner
```
pinned_runner  FK -> harness.Runner, null=True, SET_NULL, related_name="pinned_turns"
```
A hard pin: only this runner may claim the turn. Pinned turns bypass assignments and
capabilities but **never** the tenant gate, `one_executing_turn_per_agent`, or
`one_executing_turn_per_session`. If the pinned runner never returns, the turn
queues indefinitely — that is the meaning of "wait". SET_NULL on runner deletion
degrades the pin to normal routing rather than stranding the turn.

Producers of pins: drills, the chat "wait for X" / "continue on Y" choice, and
directed session starts.

### RunnerDrill
```
runner       FK -> Runner  (CASCADE)
agent        FK -> Agent   (CASCADE)
turn         FK -> Turn    (SET_NULL, null=True)   # latest drill turn
outcome      "pending" | "pass" | "fail"
summary      Text          # the agent's reported findings
started_at   DateTime
finished_at  DateTime null
unique (runner, agent)
```
Upserted (reset to `pending`) on each drill fan-out; resolved by the report endpoint
or by turn failure. Freshness is `finished_at` age — the UI ages badges out; the
server keeps no TTL.

### Turn.origin
New choice `ORIGIN_DRILL = "drill"` so drills are distinguishable in every existing
turn surface (ledger, lists, cost greps).

### Deprecated
`Agent.runner_preference` stops being read by routing. The field and its
`PATCH /runner-preference` endpoint remain for one release (returning a deprecation
note), then drop. `capabilities.agents` stops gating agent turns; `capabilities`
remains meaningful for `projects` (project-turn matching, unchanged) and
`sessions` (session capability, still runner-side truth — a rank in an assignment
list cannot make a chat-incapable runner chat-capable).

## Claim routing (`services.claim_next_turn`)

Candidate filtering gains, in order:

1. **Pin filter.** `Q(pinned_runner__isnull=True) | Q(pinned_runner=runner)` — a
   turn pinned elsewhere is invisible; a turn pinned here skips assignment/capability
   matching entirely (tenant gate still applies).
2. **Agent turns — assignment cascade.** Runner R may claim agent X's unpinned turn
   iff an assignment (X, R) exists AND every better-ranked runner in X's list is
   unavailable (`live_status != ONLINE or not ready`) — **or** the turn has been
   queued longer than `CASCADE_GRACE_SECONDS = 60`, which opens it to the next rank
   even when a higher rank looks available (a wedged-but-heartbeating runner must
   not stall the queue forever; mirrors the schedule-nag philosophy: the system
   degrades loudly instead of deadlocking silently). `_preference_allows` and the
   kind head-start are deleted.
3. **Session turns — binding stickiness.** If the session's `RunnerBinding.runner`
   is set and available, only that runner may claim (rank is irrelevant — the live
   context wins). If a binding exists but its runner is unavailable, *no other
   runner claims automatically*; the turn waits for the chat-side placement
   decision (below). A session turn created with an explicit placement is simply
   pinned. Sessions with **no binding at all** and no pin (fresh auto chats) route
   by the session agent's assignment order filtered to `sessions:true` runners;
   project chats fall back to today's behavior (any sessions-capable tenant
   runner).

The availability probe reads only `Runner` rows already in the tenant query — no new
round-trips on the hot path. All existing invariants are untouched: tenancy derives
from `paired_by` (#227), `busy_agents`/`busy_sessions` pre-filters and both unique
constraints stay, per-attempt atomic claim absorbs races.

## Chat UX

- **New chat:** the composer gains an optional **Run on** picker (default *Auto*),
  listing the agent's assigned `sessions:true` runners with live-status dots (project
  chats list all sessions-capable runners). Choosing one pins the session's first
  turn; the binding then forms on that runner and stickiness takes over.
  `POST /api/canopy-sessions/` accepts `runner_id`.
- **Send to a bound, available runner:** unchanged and invisible — the binding
  holder claims.
- **Send while the bound runner is offline:** the send succeeds (the message and
  turn are durable) and `ChatPage` shows a placement banner on the queued turn:
  *"⟨runner⟩ is offline — [Wait for it] [Continue on ⟨picker⟩]"*. **Wait** pins the
  turn to the offline runner. **Continue** re-pins to the chosen runner, which
  starts a fresh emdash session rehydrated from the durable transcript; the binding
  re-points on `record_session`. `POST /api/canopy-sessions/{id}/send` and a new
  `POST /api/canopy-sessions/{id}/place` (for the banner decision after the fact)
  carry `{placement: "wait" | {runner_id}}`.
- The sessions list and chat header already show `runner_name`/`running` — that
  display becomes the visible anchor of stickiness.

## Readiness drills

- `POST /api/harness/runners/{id}/drill` (owner-gated): body `{agents?: [slug]}`,
  default = all agents with an assignment to this runner. For each agent: upsert
  `RunnerDrill` to `pending` and `enqueue_turn(agent=X, origin="drill",
  pinned_runner=R, idempotency_key="drill:{R}:{X}:{uuid8}")` with the drill prompt.
- **Drill prompt contract** (server-side template): identifies the agent, instructs
  a read-only doctor/preflight run (clone the agent repo first if the environment
  lacks it), forbids all outward actions, and ends by POSTing
  `{"outcome": "pass"|"fail", "summary": "..."}` to
  `POST /api/harness/drills/{drill_id}/report` with the runner environment's bearer
  token. The callback is part of the test: it proves the agent can reach canopy-web
  from that environment.
- `services.finish_turn` marks the drill `fail` (summary = result note) when a drill
  turn finishes `failed` without a report. A drill left `pending` past ~30 min is
  rendered as timed-out client-side; the server stores no timer.
- `GET /api/harness/runners/{id}/drills` returns the per-agent grid; `RunnerOut`
  gains a `drill_rollup` (`passed/failed/pending`, `last_finished_at`) for badges.
- Drills queue behind a real executing turn for the same agent (the unique
  constraint) — they never interrupt live work. A drill on a busy standby simply
  waits its turn.

## UI — the routing matrix

One component, two mounts:

- **`/supervisor`** (fleet view): a matrix — one row per agent, each row an ordered
  line of runner chips (status dot, name, kind glyph). Drag to reorder, `×` to
  remove, `+` to add from the fleet. A row with no chips renders an "unroutable"
  warning chip. Runner cards gain the drill badge ("drilled 2h ago — 5/5") and the
  **Drill** button opening the per-agent grid (outcome, age, link to the drill
  turn's ledger).
- **Agent workspace → Overview**: the same row editor for that one agent, replacing
  `RunnerOrder.tsx` (kind-based) in place.

Every mutation is one `PUT /api/agents/{slug}/runners` with the full ordered list —
optimistic UI, no per-chip endpoints.

## API summary

| Route | Change |
|---|---|
| `GET/PUT /api/agents/{slug}/runners` | new — read/replace the ordered assignment list |
| `POST /api/harness/runners/{id}/drill` | new — fan out drill turns |
| `GET /api/harness/runners/{id}/drills` | new — per-agent drill grid |
| `POST /api/harness/drills/{id}/report` | new — agent-callback outcome report |
| `POST /api/canopy-sessions/` | `runner_id?` added |
| `POST /api/canopy-sessions/{id}/send` / new `/place` | `placement?` added |
| `GET /api/harness/runners/` (`RunnerOut`) | `drill_rollup` added |
| `PATCH /api/agents/{slug}/runner-preference` | deprecated (one release), then removed |

Schemas in Pydantic per house rules; regenerate `frontend/src/api/generated.ts`
(`npm run gen:api`) and commit.

## Migration & rollout

1. Additive migration: `RunnerAssignment`, `RunnerDrill`, `Turn.pinned_runner`,
   `origin` choice.
2. **Seed data migration:** for each agent, create assignments for every non-retired
   runner whose `capabilities.agents` contains it, ordered by the agent's current
   `runner_preference` kind order (unlisted kinds after listed ones, then by
   pairing date). Today's live state — all five agents on `jj-mbp-cdp` — becomes
   five rank-0 rows; nothing changes behavior at cutover.
3. Claim path switches to assignments in the same deploy (the seed guarantees
   continuity). Standby runners keep their empty capabilities harmlessly; the
   operator then adds them at rank 1+/2+ via the matrix.
4. One release later: drop `runner_preference` reads, then the field.

## Testing

- **Claim cascade:** rank-0 available → rank-1 never claims; rank-0 offline →
  rank-1 claims; both offline → rank-2; grace expiry opens the next rank while
  rank-0 is online-but-stuck; unassigned runner never claims; unassigned agent's
  turns match no one.
- **Pins:** pinned turn invisible to other runners; pinned claim bypasses
  assignments but not tenancy; pinned turn behind a busy agent waits.
- **Session stickiness:** bound+available → only binding holder; bound+offline →
  nobody until placed; `wait` pins home; `continue` pins away and binding re-points
  after `record_session`; fresh agent chat follows assignment order ∩
  `sessions:true`; project chat unchanged.
- **Drills:** fan-out creates pending rows + pinned turns; report flips outcome;
  failed turn without report → fail; re-drill resets to pending.
- **API/UI:** PUT list replace round-trips; matrix reorder persists; deprecation
  path on runner-preference.

## Invariants preserved

Tenancy from `paired_by` (#227) on every path including pins; both
one-executing-turn constraints; binding `reusable_by` host semantics;
`chat_session__isnull=False` NULL-injection guard in busy-session exclusion;
`ready` (cascade Phase A) now actually consumed by the availability probe.
