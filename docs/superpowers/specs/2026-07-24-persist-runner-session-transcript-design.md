# Persist a runner session's transcript (durable, restart-proof live view)

**Status:** built (issue #373, 2026-07-24). Builds on the unified-runner-sessions
work ([[project_unified_runner_sessions]]) and supersedes the in-memory offset fix
#368 as the *durable* answer. Two implementation additions beyond the checklist
below, both forced by trap 1's "one index space" rule: `project_events` skips
origin=runner sessions (the bridged reply's durable copy comes from the transcript,
not the ledger), and migration `canopy_sessions.0010` resets runner sessions'
pre-ordinal Message rows so legacy sequential keys can't swallow incoming ordinals.

## Problem

A runner-discovered session's transcript lives in three disconnected forms, none
authoritative: the rolling 8-message **tail** on the binding (negative index), the
**backfilled** rows (`0..n`, only if you hit "Load full"), and the **live stream**
(ephemeral WS push, `seq:0..`, persists nothing — `post_session_stream` docstring:
*"Live view only — no Message rows"*). Because the live stream isn't persisted, the
phone silently misses messages written while the chat is closed, and on runner
restart the in-memory resume point is lost (#368 fixed the *within-lifetime* case
only). Jonathan's steer (2026-07-24): we already ship the content over the wire
while you watch, so persisting it is nearly free; and rows die with the session
(`Message.session` = CASCADE), so storage growth isn't a concern.

## The model: the transcript is the single source of truth for a runner session

Key everything on the **transcript record ordinal** — the raw index into the
`.jsonl` (stable, append-only; `chat_bridge.read_records` reads the whole file). For
`origin=runner` sessions, `Message.turn_index = that ordinal`. Then the live stream
(forward) and backfill (older) are the SAME rows by identity — idempotent
`get_or_create(session, turn_index=ordinal)`, no collision, `order_by("turn_index")`
and the existing `UniqueConstraint(session, turn_index)` keep working. Web sessions
are unchanged (no transcript; `_next_index` as today).

- **Stream (steady state):** while attached, the runner ships each new conversational
  record (user+assistant) with its ordinal → server persists AND fans out the
  assistant frames.
- **Catch-up on attach:** `GET /streams` returns `last_index` (server's max
  turn_index for the session); the runner ships everything after it. Restart- and
  failover-proof because the resume marker is server-side, not a laptop offset.
- **Backfill ("Load full"):** ships the FULL transcript with ordinals; fills the
  *older* rows the stream never saw. `request_backfill` returns `ready` iff the first
  row is `turn_index==0` (we have the start), else `requested`.

## Why `send_message` must change (TRAP 1 + 2)

A runner session's user messages exist in two places: canopy's `send_message` row
(keyed `_next_index`) AND the transcript the runner types them into (keyed ordinal).
A session can hold only ONE index space, so both can't persist — they'd duplicate or
mis-order. And if we persist assistant records but NOT user records, the tail
fallback stops the instant any row exists (`if not messages:` in both `api.get_session`
and `consumers._snapshot`), so the **human side of an emdash-driven session vanishes**
(e.g. you'd see the agent's replies but not your own questions).

Resolution: for `origin=runner`, `send_message` does NOT author a durable user row —
the transcript is the sole source. Safe because the frontend already echoes the
user's message **optimistically** from `draft.committed` (`sessionReducer.ts:148`), so
there's no UX regression; the durable copy arrives via the transcript within a bridge
tick, and a reconnect snapshot replaces the optimistic one cleanly (user records
aren't live-pushed, only assistant text is, so no in-session double-render). The send
handler returns a transient (unsaved) `MessageOut` to keep its contract.

## Why this is staged, not a closeout change (TRAP 3)

Server (ECS) and the laptop runner (git pull + `launchctl kickstart`) can't deploy
atomically. `send_message`-skips-user and runner-ships-user-records MUST land
together — every intermediate state otherwise loses one side of the conversation for
a window. There is **no server-only stage that doesn't regress something** (persisting
assistant rows alone kills the tail's human side). This is a coordinated
multi-package feature with a deploy-ordering constraint — the exact "coordinated
change ships broken" pattern that produced ~8 prod bugs in the unified-sessions work,
each caught only by using the app. It deserves staged build + prod verification, not
a cram.

## Implementation checklist

**Wire (backward-compatible; `index` defaults so an old runner still works):**
- `LiveEventIn.index: int = -1`, `BackfillMessageIn.index: int = -1` (−1 ⇒ server assigns sequentially).
- `StreamDescriptorOut.last_index: int | None`.

**Server (`apps/canopy_sessions`):**
- `persist_transcript_rows(session, rows)` — `get_or_create(session, turn_index=index or _next_index)`; user+assistant+tool; idempotent.
- `post_session_stream` → persist (via helper) then fan out.
- `write_backfill` → use the helper (upsert-fill; drop "skip if any rows").
- `request_backfill` → `ready` iff first row `turn_index==0`, else `requested`.
- `send_message` → for `origin=runner`, skip the durable user row; return a transient `MessageOut`; still enqueue the turn.
- `list_streams` → `last_index = max(turn_index)` per desired session.

**Runner (`packages/canopy_runner`):**
- `chat_bridge.conversational_messages(records, since)` → `[{index, role, text}]` for records with raw file index > `since` (unifies `transcript_messages` + `new_assistant_texts`).
- `_sync_session_streams` → seed `since` from the descriptor's `last_index`; ship conversational messages after it with ordinals; drop the in-memory `_stream_state` offset hack (superseded by the server marker).
- `_drain_backfills` → ship the full transcript with ordinals.
- `Client.post_session_stream` / `post_session_backfill` → include `index`.

**Verify:** transport-parity (`test_transcript_parity.py` already guards REST↔WS); a
new test: attach → stream persists → detach → agent writes while away → re-attach →
catch-up fills the gap from `last_index` → both REST and WS show the complete,
correctly-ordered conversation, no dup of the user's own sends. Then deploy server,
pull+restart runner, and verify on prod (open a discovered session on the phone, watch
it accrue and survive a close/reopen and a runner restart).

## Not doing / deferred

- Persisting to disk on the runner (the offset band-aid) — rejected: fixes only
  runner-restart, not server-down/failover, and adds laptop state. This spec's
  server-marker approach subsumes it.
- Tool-call rendering fidelity in the persisted rows — persist tool records too, but
  the frontend's `ToolCallPair` rendering from persisted rows is a follow-up.
