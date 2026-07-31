# Closing a session from the phone

**Date:** 2026-07-30
**Status:** design
**Surfaces:** `/supervisor` → Sessions, `/w/:ws/chat`, `ChatPage`

## The problem

The supervisor's Sessions list has no end you can reach from a phone. A session
leaves the list when its emdash task stops being reported, and the only way to
stop it being reported is to go to the laptop and delete the task by hand. So the
list accumulates work you are done with, and the surface built for a phone requires
a desk.

There is already an `/archive` endpoint. It is not the answer on its own, and the
reason is the whole design:

> `apps/harness/services.py:1523-1529` — a session report **un-archives** anything
> re-reported as open, deliberately, so a task you reopen in emdash comes back.

The runner re-reports its entire open-task set on a guaranteed ~10s heartbeat. So a
server-only archive of a live local session survives about ten seconds. Closing has
to reach emdash, and the report is what confirms it.

## What "close" means

Confirmed with the operator: close means *delete the emdash task*. The context menu
offers Archive as well — found by probe in emdash 1.1.40 on 2026-07-31 — but delete
is the designed behaviour: it is verifiable by querying the sidebar before returning,
so the close is not optimistic. Archive remains unexplored as a possible gentler
close; this design chose delete. It is not undoable in emdash.

It is not destructive to the record. Canopy keeps the `Session` row, its `Turn`s and
their event ledger, and the Claude Code transcript — which lives under
`~/.claude/projects`, resolves by cwd-encoded path, and is never deleted by Claude
Code. A closed session stays readable and re-derivable via `reset`/`backfill`. You
lose the ability to *resume* it, which is the point.

## Design

### 1. One verb, two branches

`POST /api/canopy-sessions/{id}/close`, member-gated like every other session route.

It asks exactly one question — *is a runner reporting an emdash task for this
session?* — and branches:

**Runner-observed (laptop/emdash).** The server relays a `close_session` control
frame down `ws/runner/{runner_id}/` and **writes nothing**. Returns
`{ok: true, closing: true}`. The runner deletes the task and reports; the report
retires the row.

**Not runner-observed (cloud, or a web chat that never bound).** Nothing exists on a
box to delete. The server cancels the session's non-terminal turns and archives the
row, in one request. Returns `{ok: true, closing: false}` — done, and it sticks,
because nothing will ever report it back.

Refusals are `200` with `ok:false` and a stable reason, never a 4xx — mirroring
`answer_menu` and `request_backfill`, and for the same reason: a session can go stale
between the phone rendering the list and a thumb reaching it, which is ordinary
rather than a client error.

| reason | branch | meaning |
|---|---|---|
| `unavailable` | local only | the bound runner is offline, paused, or otherwise not reachable |
| `already_closed` | both | the session is already archived |

Note there is no `unbound` refusal, unlike `answer_menu`. A session with no binding
has nothing on a box, so it is not an error case — it is the second branch.

`unavailable` **refuses up front and queues nothing.** A close that silently sits
until a box comes back is indistinguishable from a close that worked, which is the
failure mode the composer's `PlacementBanner` exists to prevent on the send path.

### 2. The discriminator

The branch above turns on "will a report retire this row?", which is an observable
fact, so we observe it rather than infer it.

Neither existing field can answer it. `record_session` (`services.py:1314-1365`) is
called by **both** runners and stamps `session_key` *and* `live_seen_at`; the cloud
runner simply writes a Claude session UUID into `session_key` where the laptop writes
an emdash task name. `Runner.kind` would work today but answers a different question
(what program is this) and is already deprecated as a behavioural input.

So: **add `RunnerBinding.reported_at`**, nullable, stamped in exactly one place —
`replace_reported_sessions`, the report loop. The discriminator is
`reported_at` within `SESSION_LIVE_WINDOW`.

`live_seen_at` is left alone. It keeps its current semantics and its current two
write sites; this is a new, narrower signal beside it, not a redefinition of the
liveness clock the session list depends on.

The property this buys: a future cloud runner that starts reporting its sessions
lands in the local branch automatically, with nobody remembering to update a check.

### 3. The runner half — the closing signal finally gets a producer

New CDP command `close-task` in `cdp/emdash_control.mjs`:

1. Scroll-to-find the task, reusing `openTask`'s existing scroll. A one-shot DOM
   query only sees the visible rows — emdash virtualizes the sidebar, and a
   scrolled-out task is indistinguishable from one that never existed. This is the
   exact false negative that once duplicated a live session.
2. Open the task's menu, delete, confirm.
3. Fail loud with a distinct code if the affordance is not found.

On success the runner puts the task name into the `archived: []` list on its session
report and **reports immediately** rather than waiting for the next tick.

Everything after that already exists and is already correct:

```python
# apps/harness/services.py:1531-1538
closed = [k for k in (archived or []) if k and k not in now_keys]
```

`now_keys` winning over `archived` is not incidental — emdash task names are not
unique, and an open task must never be retired by a closed namesake.

This is the reason to prefer relay-and-report over an optimistic archive: **no new
reconcile path and no second source of truth.** The emdash task is the truth for a
local session; the control frame is a request, the report is the answer. It is the
same shape `Runner.paused` uses (one state, two control surfaces, the box reports the
edge and otherwise mirrors back down), and it deliberately avoids the tombstone shape
— a human-closed flag no report may undo — which would let canopy and emdash disagree
forever.

Cost: the row lingers for the delete + report round trip, ~1-3s. The client covers
that with a local pending state. The alternative was archiving optimistically and
letting a failed delete bounce the row back ~10s later, which is faster but briefly
shows something untrue.

### 4. Mid-turn

Session rows already carry `running`. When it is set, the phone confirms once
("Ada is working — close anyway?"), then the close **cancels the turn before deleting
the task**, so the ledger records a cancellation rather than a turn that simply stops
emitting. Idle sessions — the common case — stay one tap.

### 5. Cloud runner sessions

**No cloud-runner work at all.** Three reasons, in order of how load-bearing they are:

1. **It never reports sessions.** There is no `POST /runners/{id}/sessions` anywhere
   in `runner/ec2/cloud_runner.py`. Nothing can un-archive a cloud row, so the
   server-side archive is sufficient by itself.
2. **There is no persistent task to delete.** Each turn is a fresh
   `claude -p --resume` in `WORK_DIR/sessions/<id>`. Between turns nothing is
   running; there is no object corresponding to an emdash task.
3. **The workdir must not be deleted.** Claude Code resolves a transcript by
   cwd-encoded path (`_encode_project_dir`), so removing the workdir would
   permanently break `reset` and `backfill` for that session — the durable record.
   Closing must cost nothing recoverable.

Cloud close is therefore: cancel non-terminal turns, archive, return. The only
cloud-specific work in this whole design is §2's discriminator picking that branch.

### 6. UI

**`ChatSessionsPanel` row** — a Close affordance in an overflow, not a primary
button: the row's main action is *open*, and close is destructive-adjacent. Local
pending state while `closing: true`. The row leaves on the next `supervisor.sessions`
WS frame, which `apps/realtime/signals.py` already fans out on `sessions_reported`;
no polling is added. Because the panel is shared, this lands on `/supervisor` and
`/w/:ws/chat` from one change.

**`ChatPage` header** — the same action beside "Reset from transcript", so you can
close the session you are reading.

**Failures surface in place** on the row or in the header ("Couldn't close — jj-mbp
is offline"), not in a toast that scrolls away on a phone.

### 7. Testing

- `close` on a runner-observed session writes nothing and publishes the control
  frame; `close` on a cloud session archives and cancels its turns.
- The namesake case in `replace_reported_sessions`: a closed task name that is also
  the name of a still-open task must not retire the open one's row.
- Each refusal reason returns 200 with `ok:false`, not a 4xx.
- A pure `closeAction(session)` derivation — can it close, does it need confirming,
  what does the button say — tested without a component, mirroring
  `chatPageLogic.ts::sendBlockReason`.
- `reported_at` is stamped by the report loop and **not** by `record_session`.

## Risk

The CDP delete affordance is the only genuinely new piece. Everything else composes
from paths that exist and are tested; this is DOM archaeology against an app we do
not control, and it cannot be verified from this repo. Build it against the live
emdash first, not last. It shares a failure mode with the rest of the CDP surface —
silent drift when emdash changes its UI — and should fail loud, the way
`verify-emdash` exists to catch schema drift on the sqlite side.

If the delete fails, the task keeps being reported and the row stays. That is the
correct outcome, and it is a direct consequence of choosing relay-and-report: there
is no state to leave inconsistent.

## Rejected

**Optimistic archive + reconcile.** Archive on tap, relay, let a failed delete bounce
the row back. Instant, but briefly asserts something untrue, and the bounce is
confusing precisely when something is already wrong.

**Archive + tombstone.** A human-closed flag no report may un-archive. Never bounces,
but creates a second source of truth that can disagree with emdash permanently — the
shape `Runner.paused` was explicitly designed to avoid.

**Writing to emdash's sqlite.** The adapter is read-only by design, and emdash is an
Electron app with its own in-memory state: a row deleted underneath it would not
update the UI.
