# Chat sessions: recording, status, drafts, presence, push

How a canopy chat session (`apps/canopy_sessions`) is recorded, how "where does my
ask stand" reaches every channel, how several people share one conversation, who is
pushed about it, and how a session's liveness and transcript location are known.
Blocked-agent dialogs (menus, taps, answers) are covered in
[`blocked-agent-menus.md`](blocked-agent-menus.md); who may read or act in a session
is `apps/canopy_sessions/access.py` (see `docs/architecture/tenancy.md`).

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
  it onto the turn (`services.send_message(origin_ref=...)`, merged under the
  harness's own keys): a signal hands a receiver only the turn. Worked example:
  Slack's slash-command anchor, `apps/slack/status.py::adoption`.
- Spec: `docs/superpowers/specs/2026-09-20-unified-turn-status-design.md`.

## Recording: one path, keyed on whether a transcript exists

- `services.transcript_sourced(session)` is the one question: is a real runner
  driving this, so does a transcript exist? `Session.origin` means only "how this
  started" and never decides recording.
- **Transcript-sourced** (any session a real runner drives, whatever surface
  started it): the runner's `.jsonl` is the durable source, ordinal-keyed in
  `Message.turn_index`. A send authors **no** second copy of the user's line — it
  becomes durable as the transcript record the agent read; until then it lives in
  `Turn.prompt` and the client's optimistic echo. Streamed transcript rows persist
  through `services.persist_transcript_rows`.
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

- Right-click a message in a chat (also /ide's pane) → "Fork from here" → `POST /api/canopy-sessions/{id}/fork` (`services.fork_session`). The fork keeps everything up to the end of that message's turn — the cut is always just before the next prompt a person sent, so it never holds a tool call without its result.
- The server copies the rows (rebased below `index_offset`) and stamps `metadata.fork` = `{session_key, records, from_session}`; `resolve_session` returns `fork` until the fork records a Claude session of its own. The runner then writes the first `records` records (as `canopy_transcript.read_records` counts them) beside the source as a new session id and resumes it (laptop `desktop.fork_session`, cloud `cloud_runner._cut_fork`); its first ship replaces the copied rows.
- **Same worktree.** Files are shared, not forked. The app channel (`.canopy-desktop`) belongs to the session named in `seeded`, so a fork never takes the source's channel (`desktop.channel_held_for`), and closing one keeps a worktree another open session still uses.
- On a cloud runner every turn of a fork runs in its SOURCE's directory: `resolve_session` returns `worktree_of` (the root of a chain of forks) and `_turn_cwd` keys on it. The stream finds a fork's transcript by its session uuid, since it is not in the fork's own directory.
- Cloud runners and the laptop `claude-cli` runtime only (`services.fork_runner_supported`): emdash and the Claude app own their sessions. The menu is offered only there (`MessageContextMenu.canFork`); elsewhere a right-click is the browser's own.
- The fork opens at once. In /ide every session sits in its own place; a session another was started from (`parent_session_id`: a fork, a Slack branch, a dispatched turn's session) also shows each child as a grey link row underneath, foldable per parent, that opens the real session (`ideTree.childLinks`). Links come from the viewer's own session list, so a child they cannot read is never linked. A runner-made session for a dispatched turn takes the turn's parent and the parent chat's people (`harness.services._thread_session`).

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
- **A send only enqueues.** `services.queued_messages` (derived, never stored) shows
  every watcher the sends not yet in the transcript; REST and contact sends carry a
  `client_id` so the sender's optimistic row dedupes against the queued entry.
- **Stop cancels every live turn; Cancel withdraws one send.** Stop (the button, or
  Esc in the composer while the agent has the floor) is `services.stop_session`.
  The socket action `chat.unqueue {turn_id}` is `services.unqueue_send`: your OWN
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
  (`services._publish_user_message` → `consumers.chat_user_message`) so peers see a
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
- **Picking a box** for a project chat (the "Run on" picker's `runner_id`, a send's
  `placement`, the banner's `/place`, a move to a cloud box) needs you to administer
  it (`canopy_sessions.services.may_place_project_chat`); the picker offers only
  those. For a chat with an agent it is `agents.access.may_pin_runner`.
- **A directed-only box** (`capabilities.directed`, a test box from `up.sh --test`)
  is still offered, labelled "directed only": a chat started on it binds to it, and
  it takes nothing else.

## Session liveness is polled

- `RunnerBinding.runner` is **identity** — which box the session lives on; a report
  never nulls it. `RunnerBinding.live_seen_at` is **liveness**, read against
  `staleness.SESSION_LIVE_WINDOW` (3 min). `reported_at` is stamped only by the
  report loop (`harness.services.replace_reported_sessions`), so "a runner is
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
- `GET /api/canopy-sessions/?reply=true` stamps every row with `feed_status`; the
  Feed only sorts and counts that verdict. The "is asking" / "is done" pushes go to
  the first candidate the session is on the Feed for
  (`apps/push/services.py::_question_audience` / `_finish_audience`). Don't add a
  second rule in the browser or in push.
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
