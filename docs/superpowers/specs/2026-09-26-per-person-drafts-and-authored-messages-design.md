# Per-person drafts and authored messages in a chat session

**Status:** implemented on branch emdash/multi-player-epcv1; not yet merged.

## Problem

A chat session has ONE shared draft (`Draft`, constraint `one_open_draft_per_session`).
Whoever typed last holds a derived 2-second soft lock (`drafts.lock_holder`); anyone
else sees "X is writing" and must wait or take the box over. Two people who want to
say something at the same time collide in one textbox.

Two further gaps make a multi-person session read badly even when nobody collides:

- **No message has an author.** `Message` has no author field, `message_dto` carries
  none, and `MessageItem` renders every user line as the same right-aligned bubble.
  `Turn.initiator_*` knows who sent it, but on a transcript-sourced session (every
  runner-backed one) the durable row comes back from Claude's transcript as
  `{index, role, text}` and the link to the sender is gone.
- **A send during a running reply is delivered twice.** `send_message` enqueues a
  turn AND `_maybe_interject` pushes the same text into the running turn, so the
  agent sees it mid-turn and then again as the next turn.

## What we want

Slack-in-a-thread semantics:

1. **Everyone has their own draft.** The composer is always *yours*. The session
   creator is simply the first person typing; nobody is ever locked out and there
   is no "+" / take-over step.
2. **Everyone watching sees everyone else typing, live**, as rows labelled with the
   writer's name, between the transcript and the composer.
3. **A sent message lands in the transcript immediately, in send order, with its
   author**, marked *queued* until the agent picks it up.
4. **The agent receives messages one at a time, in send order.** A message sent
   while the agent is replying waits for that reply to finish; it is never injected
   into the running turn.
5. **Every user message shows who wrote it**, durably — surviving reload, backfill,
   and rebuild-from-transcript — and the agent is told who wrote each message.

Out of scope: viewers typing (they stay read-only), co-editing one message together,
batching queued messages into one turn, mid-turn steering.

## Design

### 1. Drafts: one per (session, user)

- `Draft` gains `author` (FK user, NOT NULL for new rows). The constraint becomes
  `UniqueConstraint(["session", "author"], condition=Q(slot="next"),
  name="one_open_draft_per_session_author")`. Migration: drop the old constraint,
  delete existing open drafts (they are transient text-in-progress; losing one is
  acceptable per the migration policy in CLAUDE.md), add the column + constraint.
- `drafts.py` shrinks: `draft_for(session, user)`, `update_draft(session, user,
  expected_version, body)` (version guard kept — it still protects one person's two
  tabs), `commit_draft(session, user) -> str`, `discard_draft(session, user)`.
  **Deleted:** `IDLE_WINDOW`, `lock_holder`, `take_over`, `DraftLockHeld`.
- Protocol (`consumers.py`, `protocol.ts`, `agui.py` passthrough). Designed so an
  OLDER client (ace-web pins `canopy-ui` 0.12.2 and talks to canopy-web's socket)
  keeps working — it simply never sees anyone else typing:
  - Client → server: `draft.update {version, body}`, `draft.discard`, `chat.send`
    — all implicitly "my draft". `draft.take_over` is accepted and ignored (an old
    client still sends it).
  - `draft.updated` (the full draft DTO, now with `author_id`) and `draft.committed`
    / `draft.discarded` go ONLY to the author's own sockets (their other tabs). An
    old client's reducer adopts any `draft.updated` into its composer and builds an
    optimistic user row out of its own box on any `draft.committed`, so a peer's
    frames must never reach it.
  - NEW `draft.typing {author: {id, name}, body, at}` goes to everyone EXCEPT the
    author: the peer view. An empty body means "stopped". Old clients ignore
    unknown events.
  - **Removed:** `draft.lock_changed`, the `draft_lock_held` error.
  - The connect snapshot keeps `active_draft` (now: the connecting user's own
    draft) and adds `peer_drafts: [{author, body, at}]` for every other non-empty
    open draft.
- Live sync keeps today's rule (`shouldSyncDraftLive`): text goes over the wire only
  while someone else is present, plus the one catch-up flush when someone joins.

### 2. Rendering drafts

- `sessionReducer` holds `drafts: Record<authorId, Draft>`. My own entry feeds
  `SendBox`; the others render as `TypingRows` ("**Alice** is typing: *…text…*",
  muted, newest-edited last) between the message list and the composer.
- A row disappears on `draft.committed` / `draft.discarded` for that author, when
  the body becomes empty, or on `presence.left` for that user.
- `SendBox` loses `canEdit`, the lock banner and the take-over button.
  `PresenceChips` drops its "is typing" line (the rows say it better).
- **Send stays enabled while the agent is replying.** Today `canSend` requires
  `!isStreaming`, so nobody can queue a message during a reply — which would make
  §3 unreachable from the UI. Send and Stop now sit side by side while a reply
  streams. `onTakeOver` / `takeOverDraft` stay in the public API as deprecated
  no-ops so a host upgrading `canopy-ui` does not break.

### 3. Send: queue only, visible to everyone

- `send_message` commits the SENDER's draft and enqueues the turn as today (FIFO
  per session is already guaranteed: `one_executing_turn_per_session` +
  `claim_next_turn` ordering by `created_at`).
- **`_maybe_interject` is deleted**, along with its call. The cloud runner's
  `runner.interject` handler becomes dead; remove it in the same change so nothing
  implies the path exists.
- **Queued messages are visible to everyone.** A transcript-sourced send writes no
  durable row, and today only the sender's client echoes it. Add
  `queued_messages(session)` — the session's turns that have not yet produced their
  transcript row, as `{turn_id, author, text, sent_at, state: queued|delivering}` —
  derived from `Turn` rows, never stored (same reasoning as `turn_status`: it is a
  function of rows that change on their own clock). It rides:
  - the **connect snapshot** (you look *because* something went quiet — the
    `session.menu` lesson), and
  - a `session.queued` frame carrying the WHOLE list, published on enqueue, on claim,
    and when a transcript user row is persisted (a delta would need a correct prior a
    just-connected client lacks).
  The client renders these in send order after the last durable message, with the
  author and a "queued" chip. The sender's optimistic echo reconciles onto its entry
  by `client_id`.
- A queued entry leaves the list when the persisted transcript row carrying its
  turn id arrives (§4). For the ledger-sourced (dev stub) path the server-written
  row already exists and gets the author directly.

### 4. Attribution: a marker in the delivered prompt

The runner receives the turn's prompt as

```
[canopy from="Alice Smith" user=42 turn=3f2a9c1e0b7d4c55a1e2f3a4b5c6d7e8]
<the person's text>
```

It is one line, machine-parseable, and deliberately visible to the agent (it
*should* know who is speaking; this complements the caller envelope rather than
replacing it). A contact gets `from="<name>" contact=<id>`.

**The marker is added at CLAIM, never stored.** `Turn.prompt` keeps the bare text,
because it has other readers that must not see the marker: Slack's "continued in
canopy" status line (`apps/slack/status.py`) and the lost-turn re-ask
(`apps/slack/services.py`), which re-sends `turn.prompt` through `send_message`
and would otherwise stack a second marker. `claim_turn` (`apps/harness/api.py`)
sets the marked text on the in-memory turn it serializes, only for a chat-session
turn with a known initiator — the same point that already attaches the claim-only
`mcp_token`.

- One module, `apps/canopy_sessions/authorship.py`, owns both directions:
  `mark(text, *, name, user_id=None, contact_id=None, turn_short) -> str` and
  `parse(text) -> (author | None, bare_text)`. Tolerant parse: only a first line
  matching the exact shape counts, so an agent or human quoting the syntax
  mid-message is never misread.
- `Message` gains `author` (JSON, nullable: `{name, user_id?, contact_id?}`) and
  `source_turn_id` (UUID, nullable). `persist_transcript_rows` runs `parse` on every
  user row: strips the marker from `plaintext`/`content.text`, sets `author` and
  `source_turn_id`. This is the single funnel for live stream, backfill and reset, so
  all three get attribution for free — including backfills of old transcripts (rows
  without a marker stay `author=None`).
- A user row with no marker was typed directly into emdash/Claude Code. It renders
  as "typed in emdash" — attributing it to the session owner would be a guess.
- The LIVE user frame (`stream_map`, `chat.user_message`) runs the same `parse`, so
  a watcher sees the stripped text + author before any reload, and the sender's
  optimistic echo still matches on text.
- `message_dto` / `MessageOut` gain `author`; regenerate `generated.ts`.
- `MessageItem`: my messages stay right-aligned primary; anyone else's are
  left-aligned-ish with a name label (exact styling decided in the plan, on design
  tokens only). Slack and the embed widget share the DTO; Slack's renderer is left
  as-is in this change.

## Error handling

- `draft.update` version mismatch: unchanged behavior (the server returns the current
  body), now scoped to one author's draft — it only fires across that person's own
  tabs.
- A marker that fails to parse is left in the text verbatim (visible, not silently
  dropped) and the row gets `author=None`.
- If `session.queued` is lost, the next snapshot or next frame (whole list) corrects
  it; no client state depends on a delta.

## Compatibility

- `canopy-ui` `src/chat` changes → version bump required (CI enforces), published on
  merge; ace-web picks it up via Dependabot. Checked: ace-web retired its own
  draft server (`ace_sessions/0009_retire_draft_and_sharetoken`) and its
  `CanopyChatPanel` connects to canopy-web's socket, so the only compatibility
  that matters is an old `canopy-ui` against the new server — handled by the frame
  routing above.
- Old runners need nothing: the marker lives in `Turn.prompt`, which every runner
  already delivers verbatim.

## Testing

- Backend: two editors update their own drafts concurrently without either raising;
  `draft.take_over` is gone; snapshot returns both drafts.
- Ordering: three sends during an executing turn → three QUEUED turns in send order,
  no `runner.interject` published, `queued_messages` lists all three in order.
- `authorship`: mark/parse round trip; quoted marker mid-text not parsed; unmarked
  row → `author=None`; `persist_transcript_rows` strips the marker and sets
  `source_turn_id` on live, backfill and reset paths.
- Frontend: reducer keeps per-author drafts and clears on commit/discard/left;
  `SendBox` never disables for another person's typing; `MessageItem` renders the
  author label; queued rows render in order and reconcile with the echo.
- Live: extend `scripts/e2e_session_chat.py` with a second-user step if a second PAT
  is practical; otherwise a manual two-browser check before calling it done.

## Deviations during implementation

- **The marker is added at claim, never stored** — already the design above (§4),
  not a deviation, but worth restating here as the first thing that would otherwise
  look like a shortcut.
- **No "typed in emdash" caption.** Every pre-existing user row (everything persisted
  before this change) also has `author=None`, so a caption keyed on "no marker"
  would mislabel all of history as emdash-typed rather than flag the one new case
  the spec meant. An unmarked row renders exactly as it did before this feature.
- **Attachments are scoped to their uploader** (`uploaded_by`), not the session. The
  pending-attachment sweep predates per-author drafts and assumed the one shared
  draft the spec removes; left as a per-session sweep, person A's send would pick up
  person B's staged files.
- **`onTakeOver` / `takeOverDraft` stay in `canopy-ui`'s public API as deprecated
  no-ops.** ace-web's `CanopyChatPanel` still passes them; removing them outright
  would break that consumer on its next Dependabot bump rather than on a change it
  can see.
- **REST and contact sends carry a `client_id`** so the sender's own optimistic
  queued row dedupes against the `queued_messages` entry the socket delivers for
  the same send, instead of showing the same message twice.
- **A ledger-sourced send now fans `chat.user_message` out on commit, to
  everyone.** Not in the original design, and found only by actually running
  the two-browser Playwright check (Task 9): §3's `queued_messages` mechanism
  and the marker parsing in §4 both assume a transcript-sourced session.
  Ledger-sourced sessions (the dev stub `CHAT_STUB_EXECUTOR` uses, and any
  pre-unification session not yet reset via `manage.py reset_chat_state`)
  write their `Message` row directly in `send_message` and, before this fix,
  published nothing beyond `draft.committed` to the sender's own tabs — a peer
  never saw the line without a reload. A transcript-sourced session gets this
  for free (`apps.harness.api.post_session_stream` fans out every "user"
  ledger row live, sender included). `services._publish_user_message` +
  `consumers.chat_user_message` is the ledger-path equivalent, built directly
  from the already-saved `Message` (no ledger round trip, so no risk of a
  second `project_events` write) and reusing the exact `chat.user_message`
  frame shape `stream_map.turn_event_to_frames` already produces for the
  transcript path — the reducer and the AG-UI projection needed no changes.
  Deliberately scoped to the ledger branch only: publishing it from the
  transcript-sourced branch too would double the row once the runner's own
  `post_session_stream` ships the same text.
