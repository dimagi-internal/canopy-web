# Supervisor inbox: items-only, actionable — design

> **Status: shipped — historical record, not current-state.** `GET /api/items/` is the fleet inbox and the `needs_you` projections are gone.
> Kept for the reasoning; check `CLAUDE.md` and the code for how it behaves today.

**Date:** 2026-07-21
**Status:** Approved (design), pending implementation plan
**Supersedes the inbox half of:** `2026-07-15-item-and-turn-design.md` (the phased
"projections retire into Item" plan). This spec collapses that phasing: we jump
straight to the endstate. No migration, no bridge, no data preservation.

## Problem

The `/supervisor` "Inbox" (and the per-agent "Needs you" rail) is neither clean
nor actionable, and is not being used in any meaningful way.

Root causes, from the current code:

1. **"Needs you" is not a first-class concept.** There is no `NeedsYou` model or
   table. `apps/agents/services.py::needs_you()` is a **function that computes an
   aggregation** on every request. The only `NeedsYou*` symbols are transport
   DTOs (`NeedsYouItem`, `NeedsYouOut`, `FleetNeedsYouOut`). It is a *label on a
   query*.

2. **The inbox is a union of four read-time projections + one real object.**
   `needs_you()` derives rows from: `SUGGESTED` tasks, human-assigned
   `IN_PROGRESS` tasks, run gates/failed steps (`_run_inbox_items`), the schedule
   nag (`schedule_nag_items`), and — the only real rows — open `Item`s
   (`_item_inbox_items`). Projections drift and cannot be acted on in place.

3. **The surface is read-only.** `WaitingOnYou.tsx` rows only link out ("Acting
   on an item inline is Phase 3" — never landed). You tap a row and get bounced
   to another surface to actually decide.

4. **Dead plumbing.** `_run_inbox_items` still computes a `notify` band that
   `needs_you()` throws away (`_run_notify`); `WaitingOnYou.tsx` comments still
   describe a three-band "review → question → notify" ranking that no longer
   exists (`NEEDS_YOU_RANK` is `['review','question']`).

## The endstate

**`Item` (`apps/harness/models.py`) is the only inbox object.** The inbox is a
single query — open Items across the caller's agents — rendered as a uniform,
fully actionable list. "Needs you" as a concept is deleted; the surface is just
**"Inbox"**.

```
Inbox = Item.objects.filter(state=open, agent__in=<my agents>)  ranked review→question
```

`Item` already has exactly the right shape: `kind ∈ {review, question}`,
`state ∈ {open, decided, dismissed}`, closed decision set
`{implement, skip, defer}`, its own `title`/`body`, and `dispatch` that enqueues
turns on `implement`. The endpoints to act already exist
(`POST /api/items/{id}/decide`, `POST /api/items/{id}/dismiss`).

### The board stays, severed from the inbox

`AgentTask` (the "who has the ball" kanban) is **kept as-is** and **no longer
feeds the inbox**. Rationale: an in-flight task is Turn-shaped work with a human
owner — a different job from "a decision you owe." Folding it into `Item` would
bloat `Item` into a work-tracker. When a task needs a human *decision*, its
producer raises an `Item`; the board keeps tracking accepted, in-flight work.

This is a boundary change only — we do not delete `AgentTask`, its routes, or its
command system. Producers keep posting tasks; they simply stop relying on the
inbox to surface task *decisions*.

## What populates the inbox

Every inbox row is a real `Item`. Producers post Items via the existing
`POST /api/agents/{slug}/items/`:

| Source | Producer | Repo | This spec's scope |
|---|---|---|---|
| Schedule nag | `apps/harness` (server-local) | canopy-web | **In scope** — reimplement as an Item producer (below) |
| Run gates / failed steps | runner `reviews.py` | agent/runner repo | Follow-on; canopy-web just displays the Items |
| Task decisions (accept/decline, human-blocked) | fleet `task-tracker` skill | agent repos | Follow-on; canopy-web just displays the Items |
| Fleet audits | Ada | ada repo | Already posts Items — unchanged |

Because the inbox is "whatever open Items exist," canopy-web does **not** block on
the follow-on producer changes. The user has accepted a possibly-sparse inbox
during that window. The schedule-nag reimplementation guarantees the inbox has
real, useful content the moment this ships.

### Schedule nag → real Item

Today `schedule_nag_items()` derives a row at read time from
`latest_occurrence_turn(schedule).status != DONE`. In the endstate it becomes an
event-driven Item:

- **Raise** a `question` Item when a scheduled occurrence becomes *owed* — concretely,
  when `release_stale_occurrence_turns` marks an occurrence `MISSED` (the existing
  claim-tick sweep in `apps/harness/services.py`). Title = schedule name, body =
  "scheduled turn missed", `origin_ref` deep-links the Schedules rail.
- **Dismiss** the open nag Item for that schedule when a later occurrence of the
  same schedule finishes (`Turn → DONE`), via the same signal path.
- Idempotency key ties the Item to `(schedule_id, slot)` so a re-fire can't
  double-raise.

This replaces `apps/harness/notify.py`'s `_inbox` channel. The channel-registry
indirection (`CHANNELS`) can stay for future email/Slack channels, but its sole
"inbox" channel now writes a real `Item` instead of returning a projection dict.

## API changes

- **Delete** `GET /api/agents/needs-you` (`fleet_needs_you`) and
  `GET /api/agents/{slug}/needs-you`.
- **Add** a fleet read on the existing items router:
  `GET /api/items/?state=open` → open Items across the caller's visible agents,
  ranked `review → question` then `created_at`. Returns `list[ItemOut]` (each
  `ItemOut` already carries `agent_slug`); the caller derives the badge count from
  the list length. Authz reuses `_visible_agent_workspace_ids`.
- **Per-agent rail** reuses the existing `GET /api/agents/{slug}/items/?state=open`.
- **Delete** DTOs `NeedsYouItem`, `NeedsYouOut`, `FleetNeedsYouOut` and the
  `needs_you`/`_task_item`/`_run_inbox_items`/`_item_inbox_items` functions in
  `apps/agents/services.py`. `schedule_nag_items` + `notify.py`'s projection dict
  path are removed in favor of the Item producer above.
- **`apps/realtime/snapshot.py`**: `waiting` per agent = count of open Items
  (`Item.objects.filter(agent=a, state=open).count()`), not `needs_you().waiting_count`.
- **`apps/push/services.py`**: waiting count = open Item count; push signals watch
  `Item` only (drop the task/gate signal receivers that fed the projection). Keep
  the "fire on increase, never decrease" snapshot semantics.

## UI changes

- **`SupervisorPage.tsx` Inbox tab**: render open Items grouped `review` /
  `question`, each row fully actionable:
  - `review` → **Implement** / **Skip** / **Defer** buttons
  - `question` → an answer field + **Answer** (decide with `comment`)
  - any row → **Dismiss**
  - Optimistic removal on decide/dismiss; on 409 (already decided) refetch; on 422
    (bad dispatch / missing answer) surface the message and keep the row open.
- **`WaitingOnYou.tsx`** is replaced by an actionable `ItemInbox` component (read-
  only linking-out is gone). The per-agent rail's `NeedsYouSection.tsx` renders the
  same Item list + the same actions — one component, both surfaces.
- **`needsYouBands.ts`**: keep only the band identity (`review`/`question` label +
  dot), now keyed off `Item.kind`. Rename to `itemBands.ts` for clarity. Delete the
  stale "notify" comments.
- **`api/agents.ts`**: remove `getFleetNeedsYou` / `NeedsYou*` types; add
  `listOpenItems()` (fleet) hitting `GET /api/items/?state=open`. Item decide/
  dismiss client calls live with the items API.
- **`useLiveSupervisor.ts`**: the live `waiting` map is now open-Item counts; the
  WS snapshot already carries per-agent waiting — only its computation changes
  (server side), not the hook's shape.

## Naming

"Needs you" is removed from code and UI. The surface is **Inbox**; its rows are
**Items**; its two bands are **Review** and **Question** (`Item.kind`). The badge
is the open-Item count. `waiting_count`/`total_waiting` field names may stay in the
snapshot/push internals (they are accurate: count of items waiting on you) but no
`NeedsYou*` symbol survives.

## Testing

- **Backend**: `GET /api/items/?state=open` aggregates across visible agents and
  excludes decided/dismissed and other members' agents (404-parity with the agents
  list). Schedule-nag Item is raised on `MISSED` and dismissed on a later `DONE`,
  idempotent per `(schedule_id, slot)`. Push/snapshot waiting counts equal open-Item
  counts. Architecture-boundary test still passes (framework-only imports).
- **Frontend**: an Item row renders the correct action set per `kind`; decide/
  dismiss removes the row optimistically; 409 refetches; 422 keeps the row and shows
  the error. Empty state renders "Nothing waiting on you."

## Out of scope (explicit)

- Deleting or restructuring `AgentTask` / the board.
- The follow-on producer changes in the runner + fleet repos (task-tracker and
  reviews.py posting Items). Tracked separately; canopy-web is display-only for them.
- Email / Slack notify channels (the registry seam remains for later).
- Data migration or backfill of existing projection state into Items — deliberately
  none; we start fresh.

## Files touched (canopy-web)

- `apps/agents/services.py` — delete `needs_you` + projection helpers.
- `apps/agents/schemas.py` — delete `NeedsYou*` DTOs.
- `apps/agents/api.py` — delete both `needs-you` routes.
- `apps/harness/items_api.py` — add fleet `GET /items/?state=open`.
- `apps/harness/notify.py` — inbox channel writes a real Item; delete projection dict.
- `apps/harness/services.py` / signals — raise/dismiss the schedule-nag Item on
  occurrence `MISSED`/`DONE`.
- `apps/realtime/snapshot.py` — waiting = open-Item count.
- `apps/push/services.py` + `apps/push/signals.py` — waiting = open-Item count; watch
  Item only.
- `frontend/src/pages/SupervisorPage.tsx` — actionable Inbox tab.
- `frontend/src/components/supervisor/WaitingOnYou.tsx` → `ItemInbox.tsx` (actionable).
- `frontend/src/pages/agents/NeedsYouSection.tsx` — render Items + actions (shared).
- `frontend/src/lib/needsYouBands.ts` → `itemBands.ts`.
- `frontend/src/api/agents.ts` — drop `getFleetNeedsYou`; add `listOpenItems`.
- Docs: `CLAUDE.md` (API + Design Decisions sections), this spec.
