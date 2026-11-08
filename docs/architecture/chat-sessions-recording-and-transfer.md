# Chat sessions: recording, forking and transfer

How a session is recorded and kept (including coaching uploads and a person deleting their own session), forking from a message, a transfer that carries the work, and secrets shared with a chat. Part of the [chat sessions](chat-sessions.md) topic; the index there lists every part.

## Recording: one path, keyed on whether a transcript exists

- A conversation runs on one agent CLI for life (`Session.cli`, set by its first claim;
  `runner-routing.md` § Which CLI runs a turn): its transcript lives in that CLI's own store.

- `services.transcript_sourced(session)` is the one question: is a real runner
  driving this, so does a transcript exist? `Session.origin` means only "how this
  started" and never decides recording.
- **Transcript-sourced** (any session a real runner drives, whatever surface
  started it): the runner's `.jsonl` is the durable source, ordinal-keyed in
  `Message.turn_index`. A send authors **no** second copy of the user's line — it
  becomes durable as the transcript record the agent read; until then it lives in
  `Turn.prompt` and the client's optimistic echo. Streamed transcript rows persist
  through `transcript_ingest.record_transcript` (`persist_transcript_rows` is its
  count-only form).
- **One identity per row, from runner to screen.** `record_transcript` decides the
  transcript identity (`fork_if_name_reused`, `ensure_transcript_identity`) and
  shifts every ordinal into its epoch (`RunnerBinding.index_offset`, or a confined
  turn's own `_turn_end_epoch`) in one place, under the binding lock; no caller
  shifts an ordinal. It returns the rows as stored, and `session_sync._ingest`
  builds each live `chat.turn_event` from them: the event carries the row's
  `turn_index` and `message_id`, which `stream_map` and the consumer use in place of
  the raw `seq` — so a live row and the same row from a reload are one row on the
  client. `seq` alone remains only on events with no row (`activity:`, `stop:`, an
  old runner's ordinal-less event).
- **What a person typed is decided once**, as a row is stored: `Message.typed`
  (`transcript_read.mark_typed`, the `is_human_input` rule). The tail's reach back
  to the person's last messages, a conversation's opening (`with_opening`) and its
  autotitle read the column. "Newest" is by `turn_index` everywhere — the tail,
  the last reply, and a session's last activity (`last_message_at`).
- **REST and the socket give a row one shape**: `message_dto` is `MessageOut` (with
  `id`) plus the streaming fields, the snapshot carries `has_more_before` /
  `oldest_loaded_turn_index` as `GET /{id}` does, and all three readers (`GET /{id}`,
  `GET /{id}/messages`, the snapshot) run `transcript_read.readable_window`.
- **Ledger-sourced** (no transcript: the dev stub, and sessions predating the flag
  until reset): rows are projected from the turn's event ledger with a dense
  counter. Don't mix the two in one session — the dense counter collides with
  transcript ordinals in `turn_index`; a reset converts.
- New `Message` writers go through `persist_transcript_rows` / `_next_index`
  (retention's `Session.retention_floor_index` is enforced there).
- **Recoverability is not listing.** A backfill resolves the transcript by worktree
  PATH under `~/.claude/projects` and never asks emdash; Claude Code never deletes
  those files, so a task emdash deleted still ships its full history. Dropping off
  the session report ends a session's listing, not its recoverability — reset
  (`POST /api/canopy-sessions/{id}/reset`, `POST /api/canopy-sessions/reset`, see
  `api-chat-and-sessions.md`) is safe for that reason.
- Spec: `docs/superpowers/specs/2026-07-24-persist-runner-session-transcript-design.md`.

### Coaching: the person's own runner uploads their sessions in full

A runner ships every ACTIVE session it backs (`GET /runners/{id}/streams`). When the
box's owner has a live `SessionReviewGrant` (they let a coach such as Trellis review
their sessions), the same list also carries their own runner-discovered sessions
that are no longer active, each flagged `ship_full` (`archived` says it is retired):
the runner ships the whole history once, through the same ordinal, idempotent
stream writes, then stops re-reading it. That is the person's machine choosing to
send — canopy still never reaches down to pull a session, and the coach reads only
stored rows. The set is `canopy_sessions.coaching.full_upload_session_ids`: never an
agent's or a contact's session, never a sensitive one (any runner requirement),
never another person's box, nothing from a sensitive-data box, and only repos the
owner opted in to (the server's `_is_canopys`, and the runner again off its
heartbeat's `report_projects`). Revoking the last grant drops them from the next
poll; ACTIVE sessions keep shipping. Rows already uploaded stay until a retention rule
or the person removes them (`DELETE /{id}/content`, § A person deleting their own session);
`reset` re-ships them and `archive` keeps them. The set has no workspace filter, so a session filed where the coach is not a member is
uploaded and still unreadable to it.

What a person is told when they tick the box is `CoachConsentDetails`
(`frontend/src/components/settings/SessionReviewersPanel.tsx`), whose comment names the
code each paragraph rests on: change them together.

### A person deleting their own session: the tombstone

`DELETE /api/canopy-sessions/{id}/content` (`canopy_sessions/content_deletion.py`; who may is
tenancy-session-access-and-retention.md § Deleting your own session's content). The rows are a cache of a file on the runner's
disk and canopy cannot delete that file, so a delete with no tombstone would undo itself on the
next ship. `Session.content_deleted_at` is the tombstone, and **every writer of a session's words
must honour it**:

- Everything a runner ships funnels through `transcript_ingest.refuses_ship`: `record_transcript`
  (rechecked under the binding and session locks a delete takes, so a racing ship stops there) and
  `session_sync._ingest`, which also drops the live fan-out. A drop answers success, so the runner
  moves on rather than retrying.
- Nothing asks for it again: `list_streams` and `list_backfills` leave it out,
  `coaching.full_upload_session_ids` too, `request_backfill` answers `unavailable`, `reset` refuses
  (`content_deleted`, before `prune_ghosts` could drop the row holding the tombstone), and a
  handoff's archive routes 404.
- The report keeps the box's liveness and writes none of its words: no tail, dialog, subagents or
  retitle, and it never revives the session. `record_session` returns early. A late turn event or
  transcript flush is dropped (`ledger`, on `Turn.content_purged_at`).
- It takes nothing new: `access.can_write`, `may_pull`, `can_share` and `may_move` are false, so
  every REST write answers 409 and the socket's drafts stop; `send_message` and `enqueue_turn` refuse.
- `DeletedTranscript` holds its Claude transcript ids, read from the binding, its turns, its usage
  rows and its handoffs. The same file shipped into a NEW session row (a box paired again, whose
  report finds no binding for the task) is dropped too, and if it is all that row holds, the row
  gets the same tombstone at once (`content_deletion.absorb`). A transcript another live session
  still uses is left out of the list.

What goes is every place a session's words are kept; `tests/test_session_content_deletion.py` walks
every model pointing at a `Session` or `Turn` and fails until a new one is placed as words, ids or
someone's own record. No search index holds session words (the list's `q` reads the title and
`session_key`). What stays: the `Session`, `Turn` and binding rows with their ids, `SessionUsage`
and `Activity`, so analytics does not change.

### A session's worktree project is not its `project`

`Session.project` is set only for an agentless repo chat; an agent chat leaves it
blank, but its worktree lives under the agent's repo
(`~/emdash/worktrees/hal/emdash/hal-canopy-web-chat-…`). The runner resolves a
transcript by `(project, task)`, so anything that tells a runner what to ship or
resolve (`/runners/{id}/streams`, `/runners/{id}/backfills`) must send
`Session.emdash_project` (`project or agent.slug`), never the bare `project` — a
blank project makes the runner skip the session silently. `RunnerBinding` caches it
as `emdash_project`, half of the unique key `(runner, emdash_project, session_key)`:
an emdash task name alone is not an identity.

## Forking a session from a message

- Right-click a message in a chat (also /ide's pane) → "Fork from here" → `POST /api/canopy-sessions/{id}/fork` (`placement.fork_session`). The fork keeps everything up to the end of that message's turn — the cut is always just before the next prompt a person sent, so it never holds a tool call without its result.
- The server copies the rows (rebased below `index_offset`) and stamps `metadata.fork` = `{session_key, records, from_session}`; `resolve_session` returns `fork` until the fork records a Claude session of its own. The runner then writes the first `records` records (as `canopy_transcript.read_records` counts them) beside the source as a new session id and resumes it (laptop `desktop.fork_session`, cloud `cloud_runner._cut_fork`); its first ship replaces the copied rows.
- **Same worktree.** Files are shared, not forked. The app channel (`.canopy-desktop`) belongs to the session named in `seeded`, so a fork never takes the source's channel (`desktop.channel_held_for`), and closing one keeps a worktree another open session still uses.
- On a cloud runner every turn of a fork runs in its SOURCE's directory: `resolve_session` returns `worktree_of` (the root of a chain of forks) and `_turn_cwd` keys on it. The stream finds a fork's transcript by its session uuid, since it is not in the fork's own directory.
- Cloud runners and the laptop `claude-cli` runtime only (`placement.fork_runner_supported`): emdash and the Claude app own their sessions. The menu is offered only there (`MessageContextMenu.canFork`); elsewhere a right-click is the browser's own.
- The fork opens at once. In /ide every session sits in its own place; a session another was started from (`parent_session_id`: a fork, a Slack branch, a dispatched turn's session) also shows each child as a grey link row underneath, foldable per parent, that opens the real session (`ideTree.childLinks`). Links come from the viewer's own session list, so a child they cannot read is never linked. A runner-made session for a dispatched turn takes the turn's parent and the parent chat's people (`harness.session_sync._thread_session`).

## A transfer carries the work

`POST /{id}/transfer` (`placement.transfer_session`) re-points the binding and hands
over the conversation; `apps/canopy_sessions/handoff.py` (`SessionHandoff`) carries
the worktree. The transcript is no substitute — shell commands change files the
transcript only names — so the SOURCE runner packs its own worktree with plain git
(`runner/canopy_runner/canopy_runner/handoff.py`, shared by both runners): branch +
HEAD, local-only commits (`git bundle`), the staged and unstaged diffs, untracked
non-ignored files (a secret-looking name such as `.env.local` is skipped and named;
ignored files never travel), optionally the Claude transcript. ≤ 24 MB packed.

- **Ask, then settle.** A reachable source (`Runner.is_reachable` — PAUSED counts: a
  box whose account is out of tokens still runs its daemon) is asked through a
  durable row it drains on the poll tick (`GET /runners/{id}/handoffs`, in
  `_report_sweep` above the pause gate; the cloud box in `_sync_session_views`). The
  transfer turn is enqueued when it answers, or after `CAPTURE_WAIT` (3 min, the
  `canopy_sessions.settle_handoffs` tick). A source that is not reachable transfers
  at once with **NO SNAPSHOT**, said in the prompt with the reason; so does a
  sensitive conversation, whose work canopy-web never holds.
- **Restore before the agent acts.** The target unpacks into the session's new
  worktree (laptop: right after emdash creates the task, or after `make_worktree`
  on the desktop/CLI runtimes; cloud: after `_turn_cwd`), refusing over tracked
  changes or onto a different repo, then checks `git status`, HEAD and the diff
  against the source's. Its report lands at `<git-dir>/canopy-handoff.json`; the
  prompt's first command waits for it (emdash types the prompt as it creates the
  task, so on a laptop the restore races the agent and the wait is what orders them).
- **Conversation.** A cloud target is given the source's transcript under its own
  Claude session id and resumes it (`resume_planned`): the epoch and `transcript_id`
  are kept, so its ordinals continue the held rows, and the prompt says the
  conversation was carried. If it cannot place it, it uses `cold_prompt`, and its
  fresh session's new id opens an epoch on first ship. emdash and the Claude app
  start their own Claude process and cannot be told to resume a placed file, so a
  laptop target gets the preamble path.
- **Close only when the work is elsewhere.** The source closes its task (the same
  `close.close_session` the web's Close uses; on a cloud box, sets the session
  directory aside) after a verified restore, or once a late snapshot is held.
  Until then its session report does not file that task as a new session
  (`handoff.suppressed_tasks`). A failed close is retried each tick and raises the
  `transfer_left_open` alert; so does a task KEPT because it holds the only copy
  (pack failed, restore mismatched, sensitive) — "Close it anyway" forces it.
- **The transfer turn runs first.** Until it has been claimed — while the source
  packs, and while it is queued — the claim holds every other turn of the session
  (`routing.transfer_held_q`) and their status reads `queued_behind` (`behind:
  "transfer"` before the transfer turn exists, `"session"` after), so nothing
  reaches the target before it has restored the work. A `record_session` from the
  source meanwhile leaves the binding on the target.
- **Onto another agent CLI.** `cli` on `POST /{id}/transfer` (and `/transfer-all`,
  and a pending `TransferRequest`) continues the conversation on another CLI its agent
  is set up for — off a box whose Claude ran out onto Codex, or back
  (`placement.cli_switch_refusal`: never a sensitive conversation off Claude Code).
  Without it a target lacking the conversation's CLI is refused, naming the `cli` that
  would work. A switch never resumes (`SessionHandoff.to_cli`, `want_transcript`
  false): at settle the conversation is held to the new CLI (`Session.cli`, its
  `cli_session_id` cleared), the transfer turn asks for it (`requested_cli`), and the
  prompt says the CLI changed. The work still travels as a snapshot.
- **Batch.** `POST /transfer-all` moves every open session on one runner (what an
  account switch does); its `dry_run` reports a target that could not run one, and
  `GET /?runner=<name>` lists them.

## Secrets shared with a chat: sealed to the machine running it

A person hands one chat a secret from its session menu ("Secrets…", `ShareSecret.tsx`); nothing is posted into the chat. canopy-web cannot read it (`apps/canopy_sessions/secrets.py`, `models.SessionSecret`):

- **Sealed in the browser.** `GET …/secrets/recipients` names the device keys of the runner the chat is bound to (`session.runner_binding.runner`): each ACTIVE key with an approved ECDH key. The browser seals the value to them (`zks1`, zk-v1 §6, bound to `session:<id>` where an agent id goes, at kv 0; targets recomputed from the keys) and sends only `value_zk`. A plaintext `value` is 422 `sealed_only`; a value sealed to a key that machine does not hold is 409 `stale_recipients`.
- **Refused with a sentence** when nothing can open it: the chat is on no machine yet (409 `no_runner`), or its machine has no approved ECDH key (409 `runner_has_no_ecdh_key`). The form shows the same sentence before anyone types.
- **Read only by the chat's session.** `canopy secret list|exec` presents the chat key (`X-Canopy-Chat-Key`); `GET /api/session-secrets/key/{name}?sealed=true` returns the sealed value and its binding, and the CLI opens it with the box's own ECDH key (`cred_resolve.open_sealed_for_device`). Without `sealed=true` — a CLI from before sealing — it is 409 `update_canopy`. A chat moved to another machine cannot open what was sealed to the old one: share it again.
- **Kept:** 30 minutes from sharing (`TTL`, purged on the off-request clock), a `session.secret.resolved` event per read with the name only, never a `Message`.
- **Honest limit:** canopy-web names the keys at the moment of sharing, so a canopy-web compromised at that moment could name a key of its own; nothing it stores can be read by it afterwards. The form shows the machine and its key's fingerprint.
