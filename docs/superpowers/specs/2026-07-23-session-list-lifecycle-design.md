# Session list lifecycle: mirror emdash, derive the residue — design

**Date:** 2026-07-23
**Status:** Approved (design), pending implementation plan
**Builds on:** `2026-07-22-reusable-chat-kit-design.md` (the unified Sessions list
landed as Plan 4 / PR #355). This spec gives that list an **end** — today it only
ever grows.

## Problem

Since PR #355 the supervisor's Sessions tab renders `ChatSessionsPanel`, backed by
`GET /api/canopy-sessions/`. That query is **append-only**: no delete route, no
archive route, no status filter, no limit. A session row is created the first time
any runner reports an emdash task and then lives forever.

From the current code:

1. **Nothing removes a row.** `apps/canopy_sessions/api.py::list_sessions` filters
   only on workspace visibility and `created_by | runner_binding__isnull=False`.
   The panel re-fetches the whole unbounded list every 20s
   (`ChatSessionsPanel.tsx`).

2. **`Session.status = archived` is dead.** The model defines it
   (`apps/canopy_sessions/models.py:19`), `_out` ships it, the row renders
   `· archived` — but nothing ever sets it and the list never filters on it.

3. **Canopy cannot tell "you closed it" from "I lost sight of it."** The runner
   filters `archived_at IS NULL` (`canopy_runner/emdash.py:148`) and discards the
   fact. Server-side, a task that falls off a report just has its live pointer
   cleared (`apps/harness/services.py:757`) — the same observable state as a dead
   runner, a renamed column, or a task pushed past `LIMIT 30`. Because those cases
   are indistinguishable, nothing can safely retire a row.

4. **An empty report blanks everything, silently.** `list_open_sessions` swallows
   every `sqlite3.Error` and returns `[]` (`emdash.py:155`). The 10s heartbeat
   still POSTs that empty list, and `replace_reported_sessions` clears **every**
   binding for that runner. `canopy_runner/main.py:433` names this exact failure
   ("quietly degrades the runner into spawning duplicate sessions and blanking the
   supervisor, with nothing in the log") — `verify-emdash` exists to catch it
   after the fact, but the runtime path still can't distinguish "I read zero open
   tasks" from "I could not read."

5. **Silent truncation.** `LIMIT 30` on the report drops the 31st-oldest task with
   no signal to either side.

## The endstate

**Explicit signals write; staleness derives.**

```
archived  ==  Session.status == 'archived'                       (durable, written)
              OR (origin == runner AND live_seen_at < now - 3d)  (derived, read-time)
```

Closing a task in emdash — and manual archive — writes `Session.status`. The
residue (a task that vanished with no closing signal) is a query-time cutoff on
`RunnerBinding.live_seen_at`. No cron, no sweep, no background worker, no new
deploy surface. This mirrors how `Runner.live_status`
(`apps/harness/models.py:141`) already works: **what we can observe, not what was
last claimed.**

Un-archive is free: the derived half recomputes on every read, and the written
half is cleared when a task reappears in a report.

### Why 3 days is safe

`live_seen_at` is stamped on *every* reported session each tick
(`apps/harness/services.py:751`), and the runner reports every open emdash task
regardless of activity. So "unseen for 3 days" means **fell off the report** for 3
days — archived, deleted, past the cap, or the runner was down. An open-but-idle
task keeps getting refreshed indefinitely and never auto-archives.

A runner offline for 3+ days does archive its whole set; that reverses on its next
report. Reversibility is what makes the tight window affordable.

## Rejected alternatives

- **Materialize everything on write** (report handler flips `status`, a periodic
  sweep flips the rest). Needs a cron for the staleness half — a deploy surface
  this repo has deliberately avoided everywhere else (cf. the runner-fired
  scheduler in `2026-07-15-agent-scheduled-turns-design.md`).
- **Derive everything at read time** (no `status` writes at all). Zero new state
  and self-healing, but an explicit emdash-close signal has nowhere durable to
  live, and manual archive of a web chat becomes impossible.

## Components

### 1. Runner: report closure, not just openness

`canopy_runner/emdash.py` gains `list_recently_archived_tasks(db_path, limit)` —
same `tasks` table, `archived_at IS NOT NULL`, `type = 'task'`, ordered
`archived_at DESC`, capped. Returns task names only. `archived_at` is already in
`READ_SCHEMA`, so `verify-emdash` needs no change.

`_maybe_report_sessions` sends it as `archived: list[str]` alongside `sessions`.
A failure reading the archived list is fail-soft: the field is omitted and the
open-session report still goes out.

### 2. Runner: a read failure means "I know nothing"

`list_open_sessions` stops returning `[]` on `sqlite3.Error` and raises
`EmdashReadError` instead. A missing DB file still returns `[]` — that is a
legitimate "no emdash here", not a failure.

`_maybe_report_sessions` catches `EmdashReadError` and **skips the report
entirely**, logging a warning (not `debug` — this is the silent-degradation class
`verify-emdash` was built for). Nothing is cleared, because nothing was observed.

This is the highest-value change in the spec and stands alone: it is the
difference between "everything is gone" and "I could not look."

### 3. Runner: raise the report cap

`LIMIT 30` becomes `Config.session_report_limit = 100`. Under the new rule,
truncation causes auto-archive, so silent truncation is no longer merely cosmetic.

`session_tail_count` (30) stays a **separate** knob: it bounds how many
*transcripts* get read, which is the expensive part of a tick. Different concern,
different limit.

### 4. Server: apply the archive signal

`ReportSessionsIn` gains `archived: list[str] = []`.
`replace_reported_sessions` — inside the existing transaction — after the
open-session upsert:

- Sessions whose binding `session_key` is in `archived` (for this runner) get
  `Session.status = archived`.
- Any session re-appearing in `sessions` has `status` cleared back to `active`
  (un-archive on reappearance).

An entry in `archived` that matches no binding is ignored. Fail-closed: an
unparseable entry never archives a different row.

### 5. Server: one filter, one param

`GET /api/canopy-sessions/?state=active|archived|all&limit=<n>`, default
`state=active`.

`active` excludes `status=archived` **and** excludes runner-origin sessions whose
binding has not been live-seen within `SESSION_STALE_AFTER = timedelta(days=3)`.
Web-origin sessions are exempt from the staleness half — they have no runner to be
seen by, so only an explicit archive ends them. `archived` returns exactly the
complement; `all` returns everything.

`limit` defaults to 200 and is clamped by the existing
`apps.api.pagination.clamp_limit`, matching `list_messages`. It is applied
**after** the existing Python sort (running first, then `last_activity_at`
descending), not as a queryset slice — the ordering that matters is computed in
`out.sort`, so slicing the queryset first could cut a running session that a
`-created_at` slice happened to miss. `state=active` already bounds the set; the
limit is a payload backstop, not a query optimisation.

### 6. Server: manual archive

- `POST /api/canopy-sessions/{id}/archive`
- `POST /api/canopy-sessions/{id}/unarchive`

Both go through `_session_or_404`, so tenancy is gated exactly as every other
route on that router (non-member → 404, no existence leak). Idempotent: archiving
an archived session is a no-op 200.

Manual archive is the escape hatch for web chats and for force-retiring a row
without touching emdash. **Nothing deletes** — archive is always reversible.

### 7. Backfill

A data migration in `apps/canopy_sessions` sets `status = archived` on
runner-origin sessions whose binding has no live sighting within 3 days (and on
those with no binding at all). The existing labs backlog collapses on deploy.

The migration is a pure `UPDATE` with no reverse (the reverse is a no-op —
un-archive happens naturally on the next report).

### 8. Frontend

`ChatSessionsPanel` gains a **Show archived** toggle beside the existing
Recent/Project sort control, passing `state` to `listSessions()`. The 20s refresh
keeps whatever state is selected. Archived rows render with the existing
`· archived` treatment already present in the row subtitle.

Regenerate `frontend/src/api/generated.ts` (`npm run gen:api`) — this touches
`apps/canopy_sessions/schemas.py` and `apps/harness/schemas.py`, so the
`regen-openapi.yml` check will fail the PR otherwise.

## Data flow

```
emdash sqlite
  ├─ open tasks     (archived_at IS NULL,  type='task', LIMIT 100)  ─┐
  └─ archived tasks (archived_at NOT NULL, type='task', LIMIT 100)  ─┤
                                                                     │  read fails?
                                                                     │  → skip report
                                       POST /runners/{id}/sessions ──┘     (nothing cleared)
                                                     │
                         replace_reported_sessions ──┤ upsert open  → live_seen_at = now, status=active
                                                     └ mark archived → status=archived
                                                     │
                 GET /api/canopy-sessions/?state=active
                   status != archived  AND  (origin=web OR live_seen_at >= now-3d)
                                                     │
                                        ChatSessionsPanel (+ Show archived)
```

## Error handling

- **Runner → server:** an archived-list read failure omits the field; an
  open-session read failure skips the whole report. Neither raises out of the
  tick loop.
- **Server → data:** unknown `archived` entries are ignored. The archive write
  shares `replace_reported_sessions`'s existing `@transaction.atomic`, so a
  partial report never half-applies.
- **Client:** the panel already keeps last-good on a failed refresh; the toggle
  does not change that.

## Testing

TDD throughout.

**Runner** (`packages/canopy_runner/tests/`)
- `list_recently_archived_tasks` returns archived tasks, excludes open ones,
  excludes `type='automation-run'`.
- `list_open_sessions` raises `EmdashReadError` on a drifted/unreadable DB, and
  still returns `[]` for a missing file.
- `_maybe_report_sessions` posts nothing when the open-session read raises.
- `_maybe_report_sessions` still posts when only the archived read fails.
- The report honours `session_report_limit`.

**Server** (`tests/`)
- A report containing `archived` archives exactly those sessions, and no others.
- A previously-archived task reappearing in `sessions` un-archives.
- `state=active` hides an archived session and a runner session unseen for 3d+;
  boundary tested either side of the cutoff.
- A web-origin session unseen for 3d+ stays in `active`.
- `state=archived` / `state=all` return the complements.
- `archive`/`unarchive` are idempotent; a non-member gets 404.
- The migration archives the stale backlog and leaves web sessions alone.

**Must stay green:** `test_session_list_unified.py`, `test_session_liveness.py`,
`test_harness_emdash_sessions.py`, `test_session_activity_and_reuse.py`,
`test_harness_session_reuse.py`, `packages/canopy_runner/tests/test_emdash.py`.

Note `record_session` creates agent/phone/project-thread sessions with
`origin=runner` (`apps/harness/services.py::_thread_session`), so they are subject
to the staleness rule — correct, since the ambient sweep reports their emdash task
too. Auto-archiving one must **not** affect reuse: `_binding_for_thread` does not
filter on `session.status`, and that must stay true (covered by
`test_harness_session_reuse.py`).

## Out of scope

- **Deleting sessions.** Archive is reversible; delete is not. No delete route.
- **Retiring `list_visible_sessions` / the `supervisor.sessions` WS frame.** Both
  are now unconsumed by the UI (`GET /api/harness/sessions` survives in
  `generated.ts` only). Real cleanup, separate blast radius, separate PR.
- **Live-pushing list changes over the WebSocket.** The 20s poll is adequate; the
  chat-kit spec already books this as a deferred follow-up.
