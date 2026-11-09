# Chat sessions: recording, forking and transfer

How a session is recorded and kept (including coaching uploads and a person deleting their own session), forking from a message, a transfer that carries the work, and secrets shared with a chat. Part of the [chat sessions](chat-sessions.md) topic; the index there lists every part.

## Recording: one path, keyed on whether a transcript exists

- A conversation runs on one agent CLI for life (`Session.cli`, set by its first claim;
  `runner-routing.md` § Which CLI runs a turn): its transcript lives in that CLI's own store.
- `services.transcript_sourced(session)` is the one question: is a real runner driving
  this, so does a transcript exist? `Session.origin` means only "how this started" and
  never decides recording.
- **Transcript-sourced**: the runner's `.jsonl` is the durable source, ordinal-keyed in
  `Message.turn_index`. A send authors **no** second copy of the user's line: it
  becomes durable as the transcript record the agent read; until then it lives in
  `Turn.prompt` and the client's optimistic echo. Streamed rows persist through
  `transcript_ingest.record_transcript` (`persist_transcript_rows` is its count-only form).
- **One identity per row, from runner to screen.** `record_transcript` decides the
  transcript identity (`fork_if_name_reused`, `ensure_transcript_identity`) and shifts
  every ordinal into its epoch (`RunnerBinding.index_offset`, or a confined turn's own
  `session_sync._turn_end_epoch`) in one place, under the binding lock; no caller shifts
  an ordinal. `session_sync._ingest` builds each live `chat.turn_event` from the stored
  rows, carrying `turn_index` and `message_id`, which `stream_map` and the consumer use
  in place of the raw `seq`, so a live row and the same row from a reload are one row on
  the client. `seq` alone remains only on events with no row.
- **What a person typed is decided once**, as a row is stored: `Message.typed`
  (`transcript_read.mark_typed`). The tail, a conversation's opening and its autotitle
  read the column. "Newest" is by `turn_index` everywhere.
- **REST and the socket give a row one shape** (`message_dto`), and all three readers
  (`GET /{id}`, `GET /{id}/messages`, the snapshot) run `transcript_read.readable_window`.
- **Ledger-sourced** (no transcript: the dev stub): rows are projected from the turn's
  event ledger with a dense counter. Do not mix the two in one session: the dense
  counter collides with transcript ordinals in `turn_index`; a reset converts.
- New `Message` writers go through `persist_transcript_rows` / `transcript_ingest._next_index`
  (`Session.retention_floor_index` is enforced there).
- **Recoverability is not listing.** A backfill resolves the transcript by worktree
  PATH under `~/.claude/projects` and never asks emdash; Claude Code never deletes
  those files, so a task emdash deleted still ships its full history. Dropping off the
  session report ends a session's listing, not its recoverability, so reset
  (`api-chat-and-sessions.md`) is safe for that reason.

### Coaching: the person's own runner uploads their sessions in full

The rule and its limits are in `apps/canopy_sessions/coaching.py` (docstring and
`full_upload_session_ids`). Invariants: the person's machine chooses to send (canopy
never reaches down to pull a session) and the coach reads only stored rows; never an
agent's or a contact's session, never a sensitive one, nothing from a sensitive-data
box, never another person's box, never one whose words were deleted; only repos the
owner opted in to (`apps/harness/api_session_sync.py::_is_canopys`, and the runner again
off its heartbeat). The set has no workspace filter, so a session filed where the coach
is not a member is uploaded and still unreadable to it. Uploaded rows stay until a
retention rule or the person removes them (below); `reset` re-ships them and `archive` keeps them.

What a person is told when they tick the box is `CoachConsentDetails`
(`frontend/src/components/settings/SessionReviewersPanel.tsx`), whose comment names the
code each paragraph rests on: change them together.

### A person deleting their own session: the tombstone

`DELETE /api/canopy-sessions/{id}/content`; who may, and what stays, is
`tenancy-session-access-and-retention.md` § Deleting your own session's content and the
docstring of `apps/canopy_sessions/content_deletion.py` (`blocker`, `delete_content`).
The rows are a cache of a file on the runner's disk and canopy cannot delete that file,
so a delete with no tombstone would undo itself on the next ship.
`Session.content_deleted_at` is the tombstone, and **every writer of a session's words
must honour it**:

- Everything a runner ships funnels through `transcript_ingest.refuses_ship`
  (`record_transcript` rechecks it under the binding and session locks a delete takes,
  so a racing ship stops there; `session_sync._ingest` also drops the live fan-out). A
  drop answers success, so the runner moves on rather than retrying.
- Nothing asks for the words again: `list_streams`, `list_backfills`,
  `coaching.full_upload_session_ids` leave it out, `request_backfill` answers
  `unavailable`, and `services.RESET_CONTENT_DELETED` makes `reset` refuse before
  `prune_ghosts` could drop the row holding the tombstone.
- The report keeps the box's liveness and writes none of its words. A late turn event or
  transcript flush is dropped.
- It takes nothing new: `access.is_content_deleted` makes `can_write`, `may_pull`,
  `can_share` and `may_move` false, so REST writes answer 409; `send.py` and
  `enqueue_turn` refuse.
- `DeletedTranscript` holds the deleted Claude transcript ids. The same file shipped into
  a NEW session row (a box paired again) is dropped too, and if it is all that row holds,
  the row gets the same tombstone at once (`content_deletion.absorb`).

`tests/test_session_content_deletion.py` walks every model pointing at a `Session` or
`Turn` and fails until a new one is placed as words, ids or someone's own record.

### A session's worktree project is not its `project`

`Session.project` is set only for an agentless repo chat; an agent chat leaves it blank,
but its worktree lives under the agent's repo. The runner resolves a transcript by
`(project, task)`, so anything that tells a runner what to ship or resolve
(`/runners/{id}/streams`, `/runners/{id}/backfills`) must send `Session.emdash_project`
(`apps/canopy_sessions/models.py`), never the bare `project`: a blank project makes the
runner skip the session silently. `RunnerBinding` caches it as `emdash_project`, half of
the unique key `(runner, emdash_project, session_key)`: an emdash task name alone is not
an identity.

## Forking a session from a message

`placement.fork_session` (docstring: the cut is always a turn boundary, so the fork never
holds a tool call without its result; it raises for a session that cannot fork). The
server copies the rows and stamps `metadata.fork`; `session_sync.resolve_session`
returns `fork` until the fork records a Claude session of its own; the runner cuts the
transcript (laptop `desktop.fork_session`, cloud `cloud_runner._cut_fork`) and its first
ship replaces the copied rows.

- **A one-session review's chat cannot be forked:** a fork would copy what that review
  read into a session that is not in its profile (`placement.fork_session`).
- **Same worktree.** Files are shared, not forked. The app channel (`.canopy-desktop`)
  belongs to the session named in `seeded`, so a fork never takes the source's channel
  (`desktop.channel_held_for`), and closing one keeps a worktree another open session still uses.
- On a cloud runner every turn of a fork runs in its SOURCE's directory:
  `resolve_session` returns `worktree_of` (the root of a chain of forks) and `_turn_cwd`
  keys on it. The stream finds a fork's transcript by its session uuid.
- Only where `placement.fork_runner_supported`; the menu is offered only there
  (`frontend/packages/canopy-ui/src/chat/messageActions.tsx::canFork`).
- /ide shows each child of a session another was started from as a link row under its
  parent (`frontend/src/pages/ide/ideTree.ts::childLinks`). Links come from the viewer's
  own session list, so a child they cannot read is never linked. A runner-made session
  for a dispatched turn takes the turn's parent and the parent chat's people
  (`harness/session_sync.py::_thread_session`).

## A transfer carries the work

`POST /{id}/transfer` (`placement.transfer_session`) re-points the binding and hands over
the conversation; `apps/canopy_sessions/handoff.py` (`SessionHandoff`; its docstring has
the ask / settle / restore / close flow) carries the worktree. The transcript is no
substitute: shell commands change files the transcript only names. The SOURCE runner
packs its own worktree with plain git (`runner/canopy_runner/canopy_runner/handoff.py`,
shared by both runners); a secret-looking name (`is_secret_name`) is skipped and named,
and ignored files never travel.

- **A source is asked if it is reachable, and PAUSED counts** (`Runner.is_reachable`): a
  box whose account is out of tokens still runs its daemon, and the pack is plain git.
  The transfer turn is enqueued when it answers, or after `handoff.CAPTURE_WAIT`
  (`canopy_sessions.settle_handoffs`). An unreachable source transfers at once with
  **NO SNAPSHOT**, said in the prompt with the reason; so does a **sensitive**
  conversation, whose work canopy-web never holds in the clear.
- **Restore before the agent acts.** The target unpacks into the new worktree, refusing
  over tracked changes or onto a different repo, then checks `git status`, HEAD and the
  diff against the source's. The prompt's first command waits for its report (emdash
  types the prompt as it creates the task, so on a laptop the restore races the agent
  and the wait is what orders them).
- **Conversation.** A cloud target is given the source's transcript under its own Claude
  session id and resumes it (`resume_planned`); if it cannot place it, it uses
  `cold_prompt`. emdash and the Claude app start their own Claude process and cannot
  resume a placed file, so a laptop target gets the preamble path.
- **Close only when the work is elsewhere.** The source closes its task after a verified
  restore, or once a late snapshot is held. Until then its session report does not file
  that task as a new session (`handoff.suppressed_tasks`). A failed close, or a task KEPT
  because it holds the only copy, raises the `transfer_left_open` alert.
- **The transfer turn runs first.** Until it has been claimed, the claim holds every
  other turn of the session (`routing.transfer_held_q`) and their status reads
  `queued_behind`, so nothing reaches the target before it has restored the work. A
  `record_session` from the source meanwhile leaves the binding on the target.
- **Onto another agent CLI.** `cli` on a transfer continues the conversation on another
  CLI its agent is set up for. `placement.cli_switch_refusal` holds the refusals; a
  sensitive conversation moves only to a CLI in `clis.SENSITIVE_CLIS`. A switch never resumes
  (`SessionHandoff.to_cli`): the conversation is held to the new CLI at settle, and the
  work still travels as a snapshot.
- **Batch.** `POST /transfer-all` moves every open session on one runner; see
  `apps/canopy_sessions/api_transfer.py`.

## Secrets shared with a chat: sealed to the machine running it

The rules, each load-bearing, are in the docstring of `apps/canopy_sessions/secrets.py`
(`models.SessionSecret`; routes in `secrets_api.py`): canopy-web cannot read the value
(sealed in the browser to the bound runner's approved device keys), the value never
enters the chat as a `Message`, only the session driving the chat gets it (the chat key,
`models.ChatKey`), and it lives for `secrets.TTL`. Gotchas the code does not make
obvious:

- A plaintext `value` is 422 `sealed_only`; a value sealed to a key that machine does not
  hold is 409 `stale_recipients`; nothing to seal to is 409 `no_runner` or
  `runner_has_no_ecdh_key`, and the share form shows the same sentence before anyone types.
- A chat moved to another machine cannot open what was sealed to the old one: share it again.
- `GET /api/session-secrets/key/{name}` without `sealed=true` is 409 `update_canopy`
  (`secrets_api.py`): plaintext is never served.
- **Honest limit:** canopy-web names the keys at the moment of sharing, so a canopy-web
  compromised at that moment could name a key of its own; nothing it stores can be read
  by it afterwards. The form shows the machine and its key's fingerprint.
