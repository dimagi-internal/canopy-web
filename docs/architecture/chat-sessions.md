# Chat sessions: recording, status, drafts, presence, push

How a canopy chat session (`apps/canopy_sessions`) is recorded, how "where does my
ask stand" reaches every channel, how several people share one conversation, who is
pushed about it, and how a session's liveness is known.
Blocked-agent dialogs (menus, taps, answers) are in
[`blocked-agent-menus.md`](blocked-agent-menus.md); who may read or act in a session
is `apps/canopy_sessions/access.py` (see `docs/architecture/tenancy.md`). The routes
are in [`api-chat-and-sessions.md`](api-chat-and-sessions.md).
Each service module states its job in its docstring: `services` (lifecycle: create,
host metadata, reset), `transcript_read`, `transcript_ingest`, `placement`
(placement, transfer, fork), `send`, `control` (dialogs, stop, close).

## Turn status: derived once, rendered by every channel

- The states, and why "queued" is five of them, are in the docstring of
  `apps/harness/turn_status.py` (`derive` / `resolve`). A channel may collapse states,
  only from the full set. Do not persist it: it is a function of rows that change on
  their own clock (`Turn.status`, runner heartbeats) and would go stale exactly when a
  runner dies.
- Renderers: `apps/slack/status.py`; `apps/canopy_sessions/status_feed.py` pushes
  `session.turn_status` onto the session socket, drawn by
  `frontend/packages/canopy-ui/src/chat/turnStatus.ts`. The embedded widget mounts the
  same `ChatPanel`.
- **"Is the last turn live" is answered once in the browser**:
  `frontend/packages/canopy-ui/src/chat/turnView.ts::useTurnView`, read by the chat
  header and `ChatPanel`. Only the NEWEST assistant row can be in flight, and not once
  the status is settled with `finished_at` at or after that row began
  (`turnView.ts::inFlightRow`), so a lost `chat.stream_complete` never holds "working" up.
- **The placement banner and the composer lock read the turn status**
  (`frontend/src/pages/chat/useRunnerPlacement.ts::heldByAbsentRunner`); the fleet list
  is read only for the "Continue on…" choices.
- **Claude Code's own error notice is not a reply.** An `isApiErrorMessage` record
  becomes an assistant row with `content.api_error`
  (`packages/canopy_transcript/canopy_transcript/rows.py::rows_for_record`), drawn as a
  "Didn't run" row (`MessageItem.tsx::apiErrorOf`), not as the agent talking.
- **A quiet `working` turn states a fact, never a verdict** ("took this 6m ago and
  nothing has come back"; never "stuck"), and never speaks over a pending dialog. The
  threshold is the same number on both sides: `turn_status.py::QUIET_AFTER_SECONDS` and
  `turnStatus.ts::QUIET_AFTER_MS`; Slack edits its line only when something changes.
- **Whether a follow-up rides the running turn** is `turn_status.midturn_of`, the
  holder-side half of `routing.may_ride`; the composer words it
  (`turnStatus.ts::composerPlaceholder`).
- **Context meter.** The window size is inferred, not reported: the transcript's model
  id never carries `[1m]`, so the bound runner's model setting
  (`SessionOut.runner_model`) decides, with a reading past 200k proving 1M
  (`packages/canopy_transcript/canopy_transcript/rows.py::record_window`,
  `frontend/packages/canopy-ui/src/chat/contextUsage.ts`). A compaction summary row
  voids the older reading.
- **What a session is doing: `apps/canopy_sessions/session_state.py`** (its docstring
  has the rule). One resolver for every list, the feed, stop-everything and the socket
  snapshot; `status_feed` says `idle` when a turn settles and the resolver agrees, so a
  missed live `session.activity` frame never leaves a client drawing "working". Never
  compute "running" from the binding alone.
- **Every channel reacts to the same three events:** enqueue
  (`harness.signals.turn_status_changed`; enqueue appends no `status` event and is the
  moment someone is watching), a `status` event row, and the throttled sweep on
  `harness.signals.sessions_reported`.
- **Rules for a new channel:**
  - the frame carries the WHOLE status, never a delta (a just-connected client has no
    correct prior);
  - it rides the connect SNAPSHOT as well as live frames;
  - it needs an explicit passthrough in `apps/canopy_sessions/agui.py`: an
    unrecognised frame is dropped silently there, and both the chat page and the
    widget use `ag-ui`.
- A channel that needs something from the originating REQUEST at post time writes it
  onto the turn (`send.send_message(origin_ref=...)`): a signal hands a receiver only
  the turn. Example: `apps/slack/status.py::adoption`.

## The rest of chat sessions, by subject

- [chat-sessions-recording-and-transfer.md](chat-sessions-recording-and-transfer.md) — How a session is recorded and kept (including coaching uploads and a person deleting their own session), forking from a message, a transfer that carries the work, and secrets shared with a chat.

## Several people in one chat: drafts and authorship

- **One draft per (session, author)** (`Draft`). A person's own `draft.updated` /
  `draft.committed` reach only their own tabs; everyone else gets `draft.typing`.
- **Draft visibility** is each person's choice for their OWN draft (`Draft.visibility`).
  `serializers.py::peer_draft_dto` is the only place the rule lives; for
  `typing`/`hidden` the words never leave the server. The mode rides its own idempotent,
  unversioned frame (`draft.set_visibility` → `drafts.py::set_visibility`), never a
  field on the version-guarded `draft.update` keystroke frame, where a stale echo can
  silently downgrade it.
- **Every send path sends through `send_paths.py::send_from`** (REST, socket, app
  views, contact API, Slack): it stamps the site's runner requirements, caps the client
  nonce, turns a refusal into a 422 on an HTTP route and runs the dev stub inline; what
  differs per path is a row of `send_paths.SEND_PATHS`. Who may send, the initiator and
  route-only follow-ups stay at the route (`tests/test_one_chat_send_path.py`).
  `runner_requirements` has no default: every send path a site's token can reach passes
  the token's requirements (`sensitive_data`) so the turn routes under the site's
  floor; a send path no site reaches (Slack) passes `()` explicitly.
- **A send only enqueues.** `send.queued_messages` (derived, never stored) shows every
  watcher the sends not yet in the transcript; REST and contact sends carry a
  `client_id` so the sender's optimistic row dedupes against the queued entry. A DONE
  send stays listed for `send.DONE_LANDING_GRACE` while no person's line has been stored
  since it was sent, so a reconnect always finds it in the queued list or in
  `send.ended_sends`.
- **The echo of your line is matched by its `client_id`.** The stored row keeps the
  send's nonce and the live `chat.user_message` carries it; the sender's page matches
  exactly, falling back to words only for a line with no nonce and only against a line
  still waiting for its echo (`sessionReducer`).
- **The open page checks itself against the server** (`ChatPage`'s `useTailCheck`,
  `canopy-ui/chat/tailDivergence`): on a difference it applies the server's rows and
  records a `chat.diverged` event (ids only).
- **A send that never lands is still shown:** `send.ended_sends` (wire key
  `cancelled`) lists every send whose turn ended without its row reaching the
  transcript, with its final `state`; a client draws anything but `cancelled` as not
  delivered.
- **Stop cancels every live turn; Cancel withdraws one send.** Stop is
  `control.stop_session`: the session's turns first (`cancel_session_turns`), else
  `interrupt_session`, which records `RunnerBinding.pending_interrupt` for the runner
  to drain on its poll tick (offered for `control.INTERRUPT_TTL`). Escape fires no
  `Stop` hook, so emdash's `agent_status` stays `working` over a stopped agent; the
  laptop runner reports `interrupted` while the transcript ends in the marker
  (`canopy_transcript.questions.turn_ended_in_interrupt`,
  `runner/canopy_runner/canopy_runner/sessions.py::annotate_engine_staleness`).
  `chat.unqueue {turn_id}` is `send.unqueue_send`: your OWN send, only while still
  `QUEUED` (a claimed one may already be in the agent's input), else refused with
  `session.error` `unqueue_refused`. Only transcript-sourced sessions have queued
  lines: a ledger send writes its Message at once.
- Pending attachments are scoped to their uploader (`uploaded_by`): one person's send
  must not carry another's staged files.
- **A runner fetches attachments through the turn it holds, never through the chat's
  read route** (`apps/harness/api_runners_credentials.py::turn_attachment_content`).
  The runner speaks as its owner, who often cannot read a teammate's chat; the claim is
  what entitles the box to the chat's sent files. A file that failed to download is
  named in the prompt, so the agent never answers as if it had seen it.
- **A pasted file is typed into the message where it was pasted, as `[Image #N]`**
  (`[File #N]` for a non-image; `SendBox`), N being its place among the staged files,
  which is the server's order (`Attachment` by `created_at`), so the host uploads one
  file at a time. Each runner saves file N as `<turn>/N-<name>` and its trailing note
  leads each path with the placeholder (`execute.attachment_lines`, mirrored in
  `cloud_runner._attachment_lines`).
- **Authorship is matched server-side, never carried on the wire.** The prompt reaches
  the agent exactly as typed (the agent learns who is asking from the caller envelope).
  `transcript_ingest.persist_transcript_rows` links a transcript row to the earliest of
  this session's claimed, unlinked chat-send turns (`authorship.is_chat_send`, created
  in the last 7 days) whose `prompt` is byte-for-byte the row's text, in send order;
  `Message.author` / `source_turn_id` come from that turn's initiator. No match →
  `author=None` (correct for a line typed straight into emdash).
- `authorship.parse` also reads the `[canopy from="…" user=N turn=<hex>]` marker that
  stored rows carry; never emit it.
- A ledger-sourced session fans `chat.user_message` out on commit
  (`send._publish_user_message`); transcript-sourced sessions get it from the streamed rows.

## Which box takes a chat's first turn

- **A bound chat** claims only on its binding's runner (stickiness, below).
- **An unbound chat with an agent** follows the agent's runner list and rules
  (`runner-routing.md` → "An agent's runner list").
- **An unbound project chat** (no agent, only `Session.project`) runs on a box that
  offers the repo in that workspace and that the workspace's runner order shares, or,
  with no order, one the person who sent it administers. A teammate's project chat in a
  workspace with no order does not land on your box; it waits (`runner-routing.md` →
  "Project chats"). A chat with neither an agent nor a repo follows the same
  order-or-your-box rule.
- **Picking a box** is one rule, `placement.may_place`, asked by every entry point that
  chooses: the "Run on" picker, a send's `placement`, the banner's `/place`, a move of
  queued turns and the cloud-box offer. For a chat with an agent it is
  `apps/agents/access.py::may_pin_runner`; for a project chat, administering the box. Nobody
  anonymous chooses a box. Slack's move-to-cloud additionally requires the clicker to
  administer the cloud box, even when they are an agent admin: a Slack identity is a
  weaker principal than a browser session (`apps/slack/services.py::_move_to_cloud`).
- **A directed-only box** (`Runner.directed`) is still offered: a chat started on it
  binds to it, and it takes nothing else.

## Session liveness is polled

- `RunnerBinding.runner` is **identity** (which box the session lives on; a report never
  nulls it). `RunnerBinding.live_seen_at` is **liveness**, read against
  `staleness.SESSION_LIVE_WINDOW`. `reported_at` is stamped only by the report loop
  (`harness/session_sync.py::replace_reported_sessions`), so "a runner is reporting an
  emdash task for this" is observable.
- The laptop runner re-reports its whole open-task set and skips the report entirely if
  it cannot read emdash, so absence is an observation and a returning runner revives
  its own sessions. Do not depend on a close signal: emdash hard-deletes closed tasks.
  The cloud runner does not report, so "unseen" must not be read as "retired" for cloud
  sessions.
- **Liveness is not routing:** a stale binding still pins its session to its holder
  (continuing elsewhere means a fresh session without context); moving it is the
  user's call via the placement banner.
- Code: `apps/canopy_sessions/staleness.py`.

## A session's lifecycle is in the event log

"What happened to session X?" is answerable from the workspace's event log alone
(`GET /api/events/?kind=session.`, admin+): every transition that starts or stops a
session, or changes who can see it or where it is listed, is ONE `Event` row (`source`
`canopy_sessions.lifecycle`, blank `key` so nothing coalesces). The kinds, who writes
each, the payload and the redaction of a sensitive conversation are in the docstring and
constants of `apps/canopy_sessions/lifecycle.py`; tests: `tests/test_session_lifecycle_events.py`.
Payloads carry ids only, never a title, prompt, message or page.

**The rule for new code:** a `save()` of `status`, `workspace` or `metadata` is observed
for free; a queryset `update()` of those columns is not, so it calls
`lifecycle.record_many` beside it. A site that knows more than the row (who asked, the
turn, that an archive is a close) wraps the write in `lifecycle.acting(...)`.

**Why a session leaves `/ide`.** The IDE lists `GET /api/me/sessions/?state=active`
(`staleness.active`). It drops out on an event (archived, moved outside your
organisations, you lost your read) or, with no event because it is derived on every
read and never written, when no runner has reported it within `SESSION_LIVE_WINDOW`
(`staleness.unseen_q`); a later report brings it straight back.

## Subagents on an `Agent` row

- A subagent writes only to `<transcript-stem>/subagents/agent-<id>.jsonl`, never to the
  session transcript, so the chat sees one `Agent` row, and a background agent's result
  returns the moment it is launched.
- `packages/canopy_transcript/canopy_transcript/subagents.py::summarize_subagents`
  reads the side files; both runners send the whole set when it changes
  (`POST /runners/{id}/session-subagents`). It is cached on `RunnerBinding.subagents`
  (words blanked for a sensitive session) and pushed as `session.subagents`.
  canopy-ui's `ToolCallPair` matches a summary to its row by `tool_use_id`
  (`subagents.ts`). A subagent's own transcript is not shipped.

## ACP is the wire shape; emdash stays the laptop executor

- The Agent Client Protocol (what emdash runs) is the shape canopy's session updates
  follow. The **cloud** runner can execute over it (`runner/canopy_acp`,
  `RUNNER_EXECUTOR`, resolved in `runner/ec2/cloud_runner.py`). The **laptop** keeps
  CDP + emdash: a canopy-spawned ACP session would be invisible to emdash.
- `tool_call_update` is a **sparse patch**: merge by `toolCallId`, never render one as a row.
- `session/load` **replays the whole conversation** as updates; a resumed turn must
  suppress them or it re-appends history to the ledger.
- See `runner/canopy_acp/README.md`.

## Push: only about your Feed

- One rule, `apps/canopy_sessions/feed.py` (its docstring and `held`), decides whether a
  session is on a person's supervisor Feed: `needs_next_prompt` and not `held`
  (`not_yours`, `parked`, `auto`). `auto` is `staleness.ran_on_its_own`, the same rule
  the cloud retire leg uses.
- `parked` needs evidence: a dialog (`waiting_on_you`) or a turn for the session queued
  or in flight (`Turn.NON_TERMINAL`, via `driving_turns`). An idle session on a
  paused/offline runner is not parked and not on the Feed; Supervisor → Runners counts
  it on the runner's row (`RunnerOut.open_sessions`).
- Why a session is open is `staleness.open_reason()`: one label from `OPEN_REASONS`, read
  off the same named checks `unseen_q` excludes on, first match wins; the per-reason
  counts sum to `open_sessions`. `unclassified` means the rule and the explanation
  disagree, which is a bug. Add a leg to `unseen_q` → add its reason.
- The box's owner and admins list the sessions open on it, facts only
  (`apps/harness/runner_sessions.py`), and close an idle one. The runner row's count and
  this list are the same `active()` set.
- On the cloud, `staleness.CLOUD_CHAT_IDLE` closes a person's auto-mode web chat (or a
  Slack-thread session, or the work session of mail a member sent,
  `staleness.person_mail_q`); a session an agent started ran on its own and closes once
  nothing is in flight, as does one from a mail trigger or an outside sender. Manual
  sessions and a dialog up hold one open.
- `GET /api/canopy-sessions/?reply=true` stamps every row with `feed_status`; the Feed
  only sorts and counts that verdict. The "is asking" / "is done" pushes go to the first
  candidate the session is on the Feed for (`apps/push/services.py::_question_audience` /
  `_finish_audience`). Do not add a second rule in the browser or in push.
- Push hears about chat turns through the harness's signals (`apps/push/signals.py`);
  the harness's turn and report paths never import push
  (`tests/test_push_listens_to_harness.py`).
- Tasks waiting on you do not push (they are not on the Feed); their per-agent count is
  kept (`AgentWaitingSnapshot`) because the live Waiting on you badge reads it.
- Pinned by `tests/test_supervisor_feed.py`.

## Viewer presence (`ws/presence/`)

- One socket per browser tab; the roster lives in Redis with a 60s field TTL
  (`apps/realtime/presence_store.py`), so closing the tab is the retirement path.
- Membership and the user's opt-out (`apps/realtime/models.py::PresencePreference`) are
  re-checked on **every** `presence.enter` (`apps/realtime/presence_consumer.py`), never cached.
- UI is the shared `canopy-ui/presence` export, mounted identically by ace-web.
- **Opt-in per page; the bar is collision, not company.** Route → page-key rules are in
  `frontend/src/presence/routes.ts`; a route with no rule yields `pageKeyFor(...) ===
  null`, which opens no socket and renders no badge. Rules exist only for co-editing
  surfaces. Adding a rule is adding presence to that page: do not add one for a read surface.
- Every rule is tenant-scoped (`/w/:workspace/…`), keyed by that workspace, so only
  members join a roster. The server also accepts the `~global` sentinel
  (`apps/realtime/presence_keys.py::GLOBAL_SENTINEL`, shared with ace-web); no
  canopy-web page uses it.

## Slash commands in the composer

- The message is sent as typed; nothing on the server rewrites a leading slash. Esc
  closes the menu and never stops the reply.
- The list is `apps/canopy_sessions/slash_commands.py` (its docstring has the two
  sources). The runner's list is a table of its own, `RunnerSlashCommands`: it is large
  and must never ride on the `Runner` row, which every poll and every `select_related` loads.
- An internal skill is never offered, from either source. Internal is Claude Code's own
  marker, `user-invocable: false` in the skill's frontmatter: not a list of names, and
  not `disable-model-invocation`, which is the opposite. The catalog keeps it as a row
  (`AgentSkill.internal`, set by `apps/agents/skill_catalog.py`), so the catalog's count
  includes it. A report without the field means not internal.
- Commands a repo defines in its own `.claude/` are not read, because emdash does not
  record where a project lives. Built-ins (`/compact`, `/model`, …) are offered only
  where ACP announces them. How a typed command runs depends on the runtime: emdash
  types it into Claude Code's TUI; claude-desktop's mod runs it; claude-cli and cloud
  `acp` run it through the ACP adapter; cloud `cli` is `claude -p`, which has skills and
  commands but few built-ins.

## How the runner drives a chat

- `CHAT_STUB_EXECUTOR` (`config/settings/base.py`; false on labs): when off, a session
  turn waits for a runner with `capabilities.sessions: true`. The laptop runner drives
  the agent's emdash session and bridges the reply (`execute_chat_turn` +
  `chat_bridge.py`). A turn finishes when the transcript hands the floor back
  (`hands_back_to_human`: any terminal `stop_reason` other than `tool_use`); idle is only
  a backstop.
- A spawned `claude` must not inherit the parent session:
  `runner/ec2/cloud_runner.py::_child_safe_env` strips `INHERITED_SESSION_MARKERS`; an
  inherited child marker turns transcript saving off. The box's own login is never stripped.
- A watched session ships every conversational transcript row, `tool_use`/`tool_result`
  included (`packages/canopy_transcript/canopy_transcript/rows.py::conversational_messages`);
  a row's payload is its stored `Message.content`, so live and history render
  identically. Tool payloads are capped at the runner. The live chat bridge stays
  prose-only (tools there would render twice).
- Optional hook path: the laptop runner installs hooks in the user-level
  `~/.claude/settings.json` with a loopback listener; `forward_sessions` forwards them as
  view-only rows (`index=-1`); switches are in `runner/canopy_runner/canopy_runner/config.py`.
- Transcript ordinals are `record * BLOCK_STRIDE + block`
  (`canopy_transcript/rows.py::compose_index`; overflow spends the record's last slot on
  a marker row). `Session.ordinal_scheme` versions this
  (`transcript_ingest.ORDINAL_SCHEME`); a stale session is reset automatically on its next write.
- The runner reports sessions change-driven; `harness.signals.sessions_reported` fans the
  owner's sessions out as `supervisor.sessions` frames.

## The transcript row contract

- What any agent CLI's producer ships and the server stores is written once, in
  `packages/canopy_transcript/canopy_transcript/contract.py` (stdlib-only; ships with
  both runners). Its docstring lists the row shape, each role's `content` fields and the
  two wire envelopes.
- Two bars: `row_problems` is the server's (tolerant: missing content or ordinal passes;
  a mistyped field, unknown role or ordinal past `ORDINAL_MAX` fails);
  `producer_problems` is what a current producer must emit.
- The server checks every incoming row, one at a time:
  `transcript_ingest.conforming_rows` (inside `persist_transcript_rows`, so stream,
  backfill and turn-end ships alike) and `session_sync._conforming_events` (so a refused
  row is not fanned out either). A refused row is logged and dropped; the rest of its batch lands.
- Conformance: `packages/canopy_transcript/tests/test_contract.py` runs every producer
  through the contract (`DURABLE_PRODUCERS`); the ACP reducer's is
  `runner/canopy_acp/tests/test_contract_conformance.py`. A new CLI's mapper adds its
  entry there and must pass unchanged.
- Claude-isms the server still reads from `content`: `record` (Claude Code's
  `isMeta`/`promptSource`/… flags, which tell a typed prompt from injected text),
  `api_error`/`signed_out`, `usage.context_tokens`, tool names in `activity.py` and the
  sensitive-data allowlist, and `transcript_noise`'s harness-marker prefixes. A second
  CLI maps onto these or goes without them; there is no `thinking` role (a reasoning
  block consumes its ordinal and emits no row).

## Live end-to-end check: `scripts/e2e_session_chat.py`

`tests/*_e2e.py` fake both ends; this script drives the real deployment, a real runner
and a real agent, and spends real tokens (`--help` has the steps, `--steps check_runner`
is the cheap connectivity check). `answer_from_the_web` asserts the agent ACTED on the
answer, not that a menu appeared or a tap returned `ok:true`.
