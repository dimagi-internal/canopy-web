# Chat sessions: recording, status, drafts, presence, push

How a canopy chat session (`apps/canopy_sessions`) is recorded, how "where does my
ask stand" reaches every channel, how several people share one conversation, who is
pushed about it, and how a session's liveness and transcript location are known.
Blocked-agent dialogs (menus, taps, answers) are covered in
[`blocked-agent-menus.md`](blocked-agent-menus.md); who may read or act in a session
is `apps/canopy_sessions/access.py` (see `docs/architecture/tenancy.md`).
The services are split by responsibility: `services` (lifecycle: create, host
metadata, transcript-sourced, reset), `transcript_read`, `transcript_ingest`,
`placement` (placement, transfer, fork), `send` and `control` (dialogs, stop,
close).

## Turn status: derived once, rendered by every channel

- `apps/harness/turn_status.py::derive` / `resolve` turn a `Turn` plus `turn_reach`
  into one of thirteen states: `picking_up`, `queued_behind`, `waiting_runner`,
  `signed_out`, `unrouted`, `working`, `blocked`, `paused` (pending) and `done`,
  `cancelled`, `missed`, `failed`, `lost` (terminal).
- **Queued is five states** (`picking_up` / `queued_behind` / `waiting_runner` /
  `signed_out` / `unrouted`) because the remedy differs — wait (for a runner, or
  for the turn ahead of it), open the laptop, sign Claude in on the box, or fix the
  agent's routing — and no client can derive those facts itself. `signed_out` is a
  runner that is online but reports `Runner.signed_out` (see runners.md § Runner
  readiness); it claims nothing, so "picking this up" would be a lie. A channel may collapse states, but only
  from the full set.
- **Not a column.** It is a function of rows that change on their own clock
  (`Turn.status`, runner heartbeats); persisting it would go stale exactly when a
  runner dies.
- Renderers: `apps/slack/status.py` (mrkdwn + Run-on-cloud button);
  `apps/canopy_sessions/status_feed.py` pushes `session.turn_status` onto the
  session socket, drawn by `canopy-ui/chat/turnStatus.ts`
  (`frontend/packages/canopy-ui/src/chat/turnStatus.ts`). The embedded widget gets
  it because it mounts the same `ChatPanel`.
- **Claude Code's own error notice is not a reply.** An `isApiErrorMessage` record
  ("Not logged in · Please run /login", a usage cap) becomes an assistant row with
  `content.api_error` (+ `signed_out`) (`canopy_transcript.rows_for_record`), carried
  on the live `chat.stream_complete` frame too; the kit draws it as a one-line
  "Didn't run" row (`MessageItem.tsx::apiErrorOf`), not as the agent talking.
- **A quiet `working` turn says so.** The status carries `claimed_at` and `last_progress_at` (the claim, or the newest ledger event) for `working`; after `QUIET_AFTER_MS` (5 min) with nothing back — counting messages on screen and a reply still streaming — the chat kit replaces silence with a fact: "cloud-ec2-2 took this 6m ago and nothing has come back from it since." No verdict ("stuck"); it never speaks over a pending dialog. Slack says the same from `TurnStatus.quiet_since` (`apps/harness/turn_status.py`, `QUIET_AFTER_SECONDS`) in Slack date tokens — "*cloud-ec2-2* took this at 15:45 and nothing has come back from it since" — so its sweep edits the line only when something changes, never every minute.
- **The composer says whether a follow-up rides the running turn.** For `working`, the status carries `midturn` (a message sent now is typed into the live session and read mid-turn, `Turn.rides_turn`) or `midturn_blocked` — `runner` (the holding box does not report `midturn`: the cloud runner), `dialog` (a parsed menu is up; it waits for the answer) or `confined` — from `turn_status.midturn_of`, the holder-side half of `routing.may_ride`. `canopy-ui/chat/turnStatus.ts::composerPlaceholder` words it; a claimed rider is listed in `queued` as `state: "midturn"` and drawn "delivered into the current turn".
- **Context meter.** Each assistant record's last row carries `content.usage` = `{context_tokens, model}` (input + both cache counts, `canopy_transcript.record_window`; never a sidechain call), stored and on `chat.stream_complete`. `ChatPanel` draws `ContextMeter` from the newest row that has it. The window is 1M when the model id or the bound runner's model setting (`SessionOut.runner_model`, reported on the heartbeat as `claude_model`) has `[1m]` or the prompt is past 200k, 200k when the runner names a model without it, else "unknown" — the transcript's model id never carries `[1m]`. A compaction summary row (`record.compact_summary`) draws as a divider and voids the older reading.
- **Every channel reacts to the same three events:** enqueue
  (`harness.signals.turn_status_changed` — enqueue appends no `status` event, and
  it is the moment someone is watching), a `status` event row, and the throttled
  sweep on `harness.signals.sessions_reported`.
- **Rules for a new channel:**
  - the frame carries the WHOLE status, never a delta (a just-connected client has
    no correct prior);
  - it rides the connect SNAPSHOT as well as live frames (people look because it
    went quiet);
  - it needs an explicit passthrough in `apps/canopy_sessions/agui.py` — an
    unrecognised frame is dropped silently there, and both the chat page and the
    widget use `ag-ui`.
- A channel that needs something from the originating REQUEST at post time writes
  it onto the turn (`send.send_message(origin_ref=...)`, merged under the
  harness's own keys): a signal hands a receiver only the turn. Worked example:
  Slack's slash-command anchor, `apps/slack/status.py::adoption`.
- Spec: `docs/superpowers/specs/2026-09-20-unified-turn-status-design.md`.

## Recording: one path, keyed on whether a transcript exists

- A conversation runs on one agent CLI for life (`Session.cli`, set by its first claim;
  `runners.md` § Which CLI runs a turn): its transcript lives in that CLI's own store.

- `services.transcript_sourced(session)` is the one question: is a real runner
  driving this, so does a transcript exist? `Session.origin` means only "how this
  started" and never decides recording.
- **Transcript-sourced** (any session a real runner drives, whatever surface
  started it): the runner's `.jsonl` is the durable source, ordinal-keyed in
  `Message.turn_index`. A send authors **no** second copy of the user's line — it
  becomes durable as the transcript record the agent read; until then it lives in
  `Turn.prompt` and the client's optimistic echo. Streamed transcript rows persist
  through `transcript_ingest.persist_transcript_rows`.
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
  `api-surface.md`) is safe for that reason.
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
poll; rows already uploaded stay until the person deletes the session.

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
- **Batch.** `POST /transfer-all` moves every open session on one runner (what an
  account switch does); `GET /?runner=<name>` lists them.

## Several people in one chat: drafts and authorship

- **One draft per (session, author)** (`Draft`). A person's own `draft.updated` /
  `draft.committed` reach only their own tabs; everyone else gets `draft.typing`
  (canopy-ui ≤0.12 adopts any `draft.updated` into its composer).
- **Draft visibility** is each person's choice for their OWN draft
  (`Draft.visibility`: `live` words-as-you-type, `typing` "is typing…" only,
  `hidden` nothing until send). `serializers.peer_draft_dto` is the only place the
  rule lives; for `typing`/`hidden` the words never leave the server. The mode rides
  its own idempotent, unversioned frame (`draft.set_visibility` →
  `drafts.set_visibility`) — never a field on the version-guarded `draft.update`
  keystroke frame, where a stale echo can silently downgrade it. The control lives
  in the chat's ⋯ menu (`ChatSessionMenu`).
- **Every door sends through `doors.send_from(door, ...)`** (REST, socket, app
  views, contact API, Slack): it stamps the site's runner requirements, caps the
  client nonce, turns a refusal into a 422 on an HTTP door and runs the dev stub
  inline; what differs per door is a row of `doors.DOORS`. Who may send, the
  initiator and door-only follow-ups stay at the door
  (`tests/test_one_chat_send_path.py`). `runner_requirements` has no default:
  every door a site's token can reach passes the token's requirements (`sensitive_data`),
  so a turn sent there routes under the site's floor; a door no site reaches
  (Slack) passes `()` explicitly.
- **A send only enqueues.** `send.queued_messages` (derived, never stored) shows
  every watcher the sends not yet in the transcript; REST and contact sends carry a
  `client_id` so the sender's optimistic row dedupes against the queued entry.
- **Stop cancels every live turn; Cancel withdraws one send.** Stop (the button, or
  Esc in the composer while the agent has the floor) is `control.stop_session`.
  The socket action `chat.unqueue {turn_id}` is `send.unqueue_send`: your OWN
  send, only while still `QUEUED` (a claimed one may already be in the agent's
  input), finished CANCELLED and the queued list re-sent; anything else is refused
  with `session.error` `unqueue_refused`. The Cancel sits on your queued line
  (`ChatPanel` → `MessageList`/`QueuedRows`). Only transcript-sourced sessions have
  queued lines: a ledger send writes its Message at once.
- Pending attachments are scoped to their uploader (`uploaded_by`): one person's
  send must not carry another's staged files.
- **A runner fetches attachments through the turn it holds, never through the
  chat's read route** (`GET /api/harness/runners/{rid}/turns/{tid}/attachments/{id}/content`).
  The runner speaks as its owner, who often cannot read a teammate's chat; the
  claim is what entitles the box to the chat's sent files. Both runners name any
  file that failed to download in the prompt, so the agent never answers as if
  it had seen it; a confined cloud session is told of files it is not given.
- **A pasted file is typed into the message where it was pasted, as `[Image #N]`**
  (`[File #N]` for a non-image; `SendBox`), N being its place among the staged
  files. Removing a chip removes its placeholder and renumbers the rest; the host
  uploads one file at a time so the server's order (`Attachment` by `created_at`)
  is that N. The message is sent as typed. Each runner saves file N as
  `<turn>/N-<name>` (pasted screenshots are all `image.png`) and its trailing
  note leads each path with its placeholder (`- [Image #2] /…/2-image.png`;
  `execute.attachment_lines`, mirrored in `cloud_runner._attachment_lines`).
- **Authorship is matched server-side, never carried on the wire.** The prompt
  reaches the agent exactly as typed (the agent learns who is asking from the
  caller envelope via the `caller_context` hook). `persist_transcript_rows` links a
  transcript row to the earliest of this session's claimed, unlinked chat-send
  turns (`authorship.is_chat_send`, sent in the last 7 days) whose `prompt` is
  byte-for-byte the row's text, in send order; `Message.author` / `source_turn_id`
  come from that turn's initiator. No match → `author=None` (correct for a line
  typed straight into emdash).
- `authorship.parse` still reads the legacy `[canopy from="…" user=N turn=<hex>]`
  marker (newline optional) for old and backfilled rows; don't emit it.
- A ledger-sourced session fans `chat.user_message` out on commit
  (`send._publish_user_message` → `consumers.chat_user_message`) so peers see a
  send live; transcript-sourced sessions get this from the streamed rows.
- `onTakeOver` / `takeOverDraft` stay in `canopy-ui`'s public API as deprecated
  no-ops — ace-web still passes them.
- Spec: `docs/superpowers/specs/2026-09-26-per-person-drafts-and-authored-messages-design.md`.

## Which box takes a chat's first turn

- **A bound chat** claims only on its binding's runner (stickiness, below).
- **An unbound chat with an agent** follows the agent's runner list and rules
  (`runners.md` → "An agent's runner list").
- **An unbound project chat** (no agent, only `Session.project`) runs on a box that
  declares the repo and that the workspace's runner order shares — or, with no
  order, one the person who sent it administers. A teammate's project chat in a
  workspace with no order does not land on your box; it waits, and `/supervisor`'s
  unclaimable report says "no runner order in <ws>, and <person> administers no box
  with <repo>" (`runners.md` → "Project chats"). A chat with neither an agent nor a
  repo follows the same order-or-your-box rule.
- **Picking a box** for a chat is one rule, `canopy_sessions.placement.may_place`,
  asked by every door that chooses: the "Run on" picker's `runner_id`, a send's
  `placement`, the banner's `/place`, a move of queued turns (`move_queued_turns`,
  Slack's move-to-cloud) and the cloud-box offer. For a chat with an agent it is
  `agents.access.may_pin_runner` (an agent admin, or a runner admin of that box);
  for a project chat, administering the box — the picker offers only those. Nobody
  anonymous chooses a box. Slack's move-to-cloud additionally requires the clicker
  to administer the cloud box, even when they are an agent admin: a Slack identity
  is a weaker principal than a browser session (`apps/slack/services.py::_move_to_cloud`).
- **A directed-only box** (`Runner.directed`, e.g. a test box from `up.sh --test`)
  is still offered, labelled "directed only": a chat started on it binds to it, and
  it takes nothing else.

## Session liveness is polled

- `RunnerBinding.runner` is **identity** — which box the session lives on; a report
  never nulls it. `RunnerBinding.live_seen_at` is **liveness**, read against
  `staleness.SESSION_LIVE_WINDOW` (3 min). `reported_at` is stamped only by the
  report loop (`harness.session_sync.replace_reported_sessions`), so "a runner is
  reporting an emdash task for this" is observable.
- The laptop runner re-reports its whole open-task set every ~10s (`session_report_seconds`) and skips
  the report entirely if it cannot read emdash, so absence is an observation and a
  returning runner revives its own sessions. Don't depend on a close signal — emdash
  hard-deletes closed tasks. The cloud runner does not report, so "unseen" must not
  be read as "retired" for cloud sessions.
- **Not extended to claim routing:** a stale binding still pins its session to its
  holder (continuing elsewhere means a fresh session without context); moving it is
  the user's call via the placement banner.
- The banner must fail **closed**: `frontend/src/components/chat/runnerEligibility.ts`
  (`isBoundRunnerOffline`) treats a runner missing from `GET /runners/` (which omits
  retired runners) as offline, not unknown.
- Code: `apps/canopy_sessions/staleness.py`. Spec:
  `docs/superpowers/specs/2026-07-24-directed-runner-routing-design.md`.

## Subagents on an `Agent` row

- A subagent writes only to `<transcript-stem>/subagents/agent-<id>.jsonl` (plus an `agent-<id>.meta.json` naming the parent's `Agent` call as `toolUseId`), never to the session transcript, so the chat sees one `Agent` row — and a background agent's result returns the moment it is launched.
- `canopy_transcript.summarize_subagents` reads the side files into one dict per subagent (`state` running/done by `hands_back_to_human`, `steps`, `last` step, times, `parent_agent_id`); both runners send the whole set from their stream loop when it changes (`POST /runners/{id}/session-subagents`). It is cached on `RunnerBinding.subagents` (words blanked for a sensitive session), pushed as `session.subagents` (AG-UI `CUSTOM`), and served in the socket snapshot and the REST detail as `subagents`.
- canopy-ui's `ToolCallPair` matches a summary to its row by `tool_use_id` (`subagents.ts`), and shows the subagent's state rather than its tool result's. Its own transcript is not shipped.

## ACP is the wire shape; emdash stays the laptop executor

- The Agent Client Protocol (`@agentclientprotocol/claude-agent-acp`, what emdash
  runs) is the shape canopy's session updates follow: `tool_call` /
  `tool_call_update` with status, `agent_message_chunk`, `agent_thought_chunk`,
  `plan`, `usage_update`, and rate-limit metadata (`_claude/rateLimit`,
  `five_hour`).
- The **cloud** runner can execute over it (`runner/canopy_acp`,
  `RUNNER_EXECUTOR=acp`; default `cli` in `runner/ec2/cloud_runner.py`). The
  **laptop** keeps CDP + emdash: a canopy-spawned ACP session would be invisible to
  emdash.
- `tool_call_update` is a **sparse patch** — merge by `toolCallId`, never render one
  as a row.
- `session/load` **replays the whole conversation** as updates; a resumed turn must
  suppress them or it re-appends history to the ledger.
- Spec: `docs/superpowers/specs/2026-07-27-acp-adoption-design.md`;
  `runner/canopy_acp/README.md`.

## Push: only about your Feed

- One rule, `apps/canopy_sessions/feed.py`, decides whether a session is on a
  person's supervisor Feed: `needs_next_prompt` (blocked on a dialog, or the agent
  had the last word and stopped) and not `held` — `not_yours` (on someone else's
  runner and they did not start it; on a cloud box an agent-opened session belongs
  to the agent's owner), `parked` (runner paused/offline) or `auto` (an agent's own
  auto-mode run, not a chat).
- `parked` needs evidence: a dialog (`waiting_on_you`) or a turn for the session
  queued/in flight (`Turn.NON_TERMINAL`, via `driving_turns`). An idle session on a
  paused/offline runner is not parked and not on the Feed; Supervisor → Runners
  counts it on the runner's row (`RunnerOut.open_sessions`). The Feed banner says
  what is held up ("2 sessions need you, but X is paused"), never a bare "waiting".
- Why a session is open is `staleness.open_reason()`: one fixed label from
  `OPEN_REASONS` (`transferring`, `reported_recently`, `turn_running`, `dialog_up`,
  `manual_turn`, `person_started`, `no_turn_found`, `unbound`, `unobservable`), read
  off the same named checks `unseen_q` excludes on, first match wins. The runner row
  carries the counts (`RunnerOut.open_sessions_by_reason`: count and oldest
  activity per reason, no session or person), and they sum to `open_sessions`.
  `unclassified` means the rule and the explanation disagree, which is a bug;
  `no_turn_found` is a cloud session nothing can finish. Add a leg to `unseen_q`
  → add its reason.
- The box's owner and admins list the sessions open on it, facts only
  (`apps/harness/runner_sessions.py`, `GET /runners/{id}/sessions/`): who started
  each (the first driving turn's initiator; an agent's own login reads as that
  agent), how, its turns, last activity and `open_reason`; and close an idle one.
  The runner row's count and this list are the same `active()` set.
- On the cloud, a person's auto-mode web chat closes after `CLOUD_CHAT_IDLE` (24h)
  with no turn created or finished; a session an agent started (its first turn's
  initiator is an agent or an agent's own login) ran on its own, so it closes once
  nothing is in flight. Manual sessions and a dialog up still hold one open.
- `GET /api/canopy-sessions/?reply=true` stamps every row with `feed_status`; the
  Feed only sorts and counts that verdict. The "is asking" / "is done" pushes go to
  the first candidate the session is on the Feed for
  (`apps/push/services.py::_question_audience` / `_finish_audience`). Don't add a
  second rule in the browser or in push.
- Push hears about chat turns through the harness's signals (`apps/push/signals.py`):
  `session_menu_changed` with `new_ask` ("is asking"), `session_turn_finished`
  ("is done") and `session_turn_enqueued` (new work drops a pending "done"). The
  harness's turn and report paths never import push
  (`tests/test_push_listens_to_harness.py`).
- Tasks waiting on you do not push (they are not on the Feed). Their per-agent count
  is still kept (`AgentWaitingSnapshot`, marked dirty from `apps/push/signals.py`)
  because the live Waiting on you badge reads it.
- Pinned by `tests/test_supervisor_feed.py`.

## Viewer presence (`ws/presence/`)

- One socket per browser tab; the roster lives in Redis (`PRESENCE_REDIS_URL`, from
  `REDIS_URL`) with a 60s field TTL (`apps/realtime/presence_store.py`), so closing
  the tab is the retirement path.
- Membership and the user's opt-out (`PresencePreference`,
  `GET|PATCH /api/me/presence-preference/`) are re-checked on **every**
  `presence.enter` (`apps/realtime/presence_consumer.py`), never cached.
- UI is the shared `canopy-ui/presence` export (`PresenceBadge` / `usePresence` /
  `pageKeyFor`), mounted identically by ace-web.
- **Opt-in per page; the bar is collision, not company.** Route → page-key rules are
  in `frontend/src/presence/routes.ts`; a route with no rule yields
  `pageKeyFor(...) === null`, which opens no socket and renders no badge. Rules
  exist only for co-editing surfaces: a chat session, a DDD narrative / run, a
  review. Adding a rule is adding presence to that page — don't add one for a read
  surface.
- Every rule is tenant-scoped (`/w/:workspace/…`), keyed by that workspace, so only
  members join a roster. The server also accepts the `~global` sentinel
  (`apps/realtime/presence_keys.py::GLOBAL_SENTINEL`, shared with ace-web) for a page
  with no workspace; no canopy-web page uses it.

## Slash commands in the composer

- Typing `/` opens a menu (canopy-ui `SendBox`, `slashCommands` prop; matching in
  `chat/slashCommands.ts`). Enter or Tab completes `/name `, then Enter sends; Esc closes
  the menu and never stops the reply. The message is sent as typed. Nothing on the server
  rewrites a leading slash.
- The list is `GET /api/canopy-sessions/{id}/slash-commands`
  (`apps/canopy_sessions/slash_commands.py`): the agent's `launchable` skills as
  `<slug>:<name>`, then what the session's bound runner reported (`RunnerSlashCommands`, a
  table of its own: the list is ~120 KB per box and must never ride on the `Runner` row,
  which every poll and every `select_related` loads).
  A session not yet on a runner offers only the agent's skills.
- Each runner reports what its sessions can run (`canopy_runner/slash_commands.py`, shared by
  the cloud runner), and sends it only when the list changes: `~/.claude/skills`,
  `~/.claude/commands`, enabled plugins as `plugin:name`, plus whatever ACP sessions announced
  (`available_commands_update`). Commands a repo defines in its own `.claude/` are not read,
  because emdash does not record where a project lives.
- Built-ins (`/compact`, `/model`, …) are offered only where ACP announces them. How a typed
  command runs depends on the runtime:
  - emdash: the text is typed into Claude Code's TUI, and its own `/` popup takes the first
    Enter.
  - claude-desktop: the mod runs it with `$.command.run`.
  - claude-cli and cloud `acp`: the ACP adapter runs it.
  - cloud `cli`: `claude -p`, which has skills and commands but few built-ins.

## How the runner drives a chat

- `CHAT_STUB_EXECUTOR` (true in dev, false on labs): when off, a session turn waits for a runner with `capabilities.sessions: true`. The laptop runner drives the agent's emdash session and bridges the reply (`execute_chat_turn` + `chat_bridge.py`); `chat_pump` advances bridges each tick, in-flight turn ids on the heartbeat renew the 900s lease. A turn finishes when the transcript hands the floor back (`hands_back_to_human`: any terminal `stop_reason` other than `tool_use`); idle is only a 15-min backstop.
- A spawned `claude` must not inherit the parent session: `runner/ec2/cloud_runner.py::_child_safe_env` strips `INHERITED_SESSION_MARKERS` (`CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_EXECPATH`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_PID`, `CLAUDE_EFFORT`) — an inherited child marker turns transcript saving off. `CLAUDE_CODE_OAUTH_TOKEN` is never stripped (it is the box's login).
- A watched session ships every conversational transcript row, `tool_use`/`tool_result` included (`packages/canopy_transcript::conversational_messages`); a row's payload is its stored `Message.content`, so live and history render identically. Tool payloads are capped at the runner (`TOOL_TEXT_MAX`, `TOOL_INPUT_STR_MAX`). The live chat bridge stays prose-only (tools there would render twice).
- Optional hook path: the laptop runner installs `PreToolUse`/`PostToolUse`/`UserPromptSubmit`/`Stop` hooks in the user-level `~/.claude/settings.json` with a loopback listener (`hook_port`, default 8787; 0 disables); `forward_sessions` (default off) forwards them as view-only rows (`index=-1`); counters ride the heartbeat every 300s.
- Transcript ordinals are `record * BLOCK_STRIDE + block` (`BLOCK_STRIDE` = 64; overflow spends the record's last slot on a marker row). `Session.ordinal_scheme` versions this; a stale session is reset automatically on its next write.
- The runner reports sessions change-driven; `harness.signals.sessions_reported` fans the owner's sessions out as `supervisor.sessions` frames (typed in `frontend/src/api/types.ws.ts`; no page consumes them today).

## Live end-to-end check: `scripts/e2e_session_chat.py`

- `tests/*_e2e.py` fake both ends; this script drives the real deployment, a real
  runner and a real agent, and spends real tokens.
- Steps: `check_runner · create_session · send_and_reply · answer_from_the_web ·
  load_full_history · scroll_back · rebuild_from_transcript · close_session`.
  `--agent` and a token (`--token` or `CANOPY_E2E_TOKEN`) are required; the session
  is pinned to this machine's runner by default (`--runner` to override) so the
  keystroke has a known destination. `--steps check_runner` is the cheap
  connectivity check; `--keep` leaves the session open.
- `answer_from_the_web` asserts the agent ACTED on the answer, not that a menu
  appeared or a tap returned `ok:true`.
- Not covered: a dialog answered at the keyboard while the phone still shows it —
  that recovery is pinned by unit tests (see `blocked-agent-menus.md`).
