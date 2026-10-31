/**
 * protocol.ts — the canonical chat WebSocket protocol (WsAction / WsEvent) and
 * the session/message/draft/participant shapes the socket carries.
 *
 * Ported from ace-web's `api/types.ws.ts`. The wire contract is IDENTICAL to
 * ace's EXCEPT that **all message/draft ids are strings** (canopy sends string
 * PKs) — see `apps/chat/serializers.py` + `apps/chat/consumers.py`.
 *
 * This module is dependency-free (types only) so a vitest run doesn't pull any
 * DOM/runtime deps.
 */

// ---------------------------------------------------------------------------
// Core enum aliases
// ---------------------------------------------------------------------------

export type MessageStatus = "pending" | "streaming" | "complete" | "error";
export type MessageRole =
  | "user"
  | "assistant"
  | "system"
  | "tool_use"
  | "tool_result";

// ---------------------------------------------------------------------------
// Session + message shapes (the `session.state` snapshot payload)
// ---------------------------------------------------------------------------

/** Who sent a human line — a canopy user or a contact, never both.
 * Mirrors `apps/canopy_sessions/authorship.py::parse`'s return shape. */
export interface MessageAuthor {
  name: string;
  user_id?: number;
  contact_id?: number;
}

export interface Message {
  /** String PK (canopy sends `str(msg.pk)`), or a synthetic stream id. */
  id: string;
  turn_index: number;
  role: MessageRole;
  content: Record<string, unknown>;
  plaintext: string;
  status: MessageStatus;
  error_detail: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  /** Who typed this line — a canopy user or a contact. Absent/null on rows
   *  from before authorship was tracked, or on the agent's own messages. */
  author?: MessageAuthor | null;
  /** MCP Apps: this tool result has a View (spec 2026-10-08 §3). Set by the
   *  server on REST rows, the snapshot and the live frame — never guessed. */
  app?: MessageAppRef | null;
  /** The agent asked for one of its declared secrets here: the host renders a
   *  seal card (`SecretRequestHostContext`). Set by the server, never guessed. */
  secret_request?: MessageSecretRequestRef | null;
  /** A SENSITIVE conversation's words, sealed to a key only its people hold (a
   *  `cc1:` envelope). `plaintext` / `content` are then metadata: the host decrypts
   *  this in the browser and hands the kit the words. Absent on ordinary rows. */
  sealed?: string | null;
}

/** `MessageOut.app` — mirrors `AppRef` in `mcpApps/AppView.tsx`. */
export interface MessageAppRef {
  tool_call_id: string;
  site: string;
  tool: string;
  resource_uri: string;
  path?: string;
}

/** `MessageOut.secret_request` — a request carries no value, ever. */
export interface MessageSecretRequestRef {
  id: string;
  agent: string;
  name: string;
}

/** How much of an in-progress message its author lets OTHERS see: the words
 *  live as typed (`live`, today's behaviour), just the fact of typing
 *  (`typing`), or nothing at all until it's sent (`hidden`). Chosen per
 *  person, per browser (persisted in localStorage) — see useSessionSocket. */
export type TypingVisibility = "live" | "typing" | "hidden";

export interface Draft {
  /** String PK (canopy sends `str(draft.pk)`). */
  id: string;
  slot: "next" | "queued";
  status: "open" | "sent" | "discarded";
  body: string;
  version: number;
  last_editor: number;
  last_edit_at: string;
  /** The draft's OWNER — distinct from `last_editor`, which is whoever most
   *  recently typed in it. Since a `draft.updated` frame now only ever
   *  reaches its author's own sockets, this is the reliable field to compare
   *  against `current_user_id` for echo-suppression; `last_editor` is kept
   *  for servers that don't send it yet. */
  author_id?: number;
  /** This author's chosen mode. Absent on an older server. */
  visibility?: TypingVisibility;
}

/** A peer's live, uncommitted draft — everyone else's box, broadcast as they
 *  type. Keyed by author, never by draft id: a client only ever needs "what
 *  is this person typing right now", and an empty `body` means they sent,
 *  discarded, or cleared it — OR its author is in `typing`/`hidden` mode,
 *  where the server withholds the words but still says whether they're
 *  typing via `typing`. */
export interface PeerDraft {
  author: { id: number; name: string };
  body: string;
  at: string | null;
  /** Whether this person is actively typing right now — the one fact the
   *  server still tells peers when `body` is withheld. Absent from an older
   *  server, where a non-empty `body` was the only signal. */
  typing?: boolean;
}

/** How full the context window was for one model call (the transcript's
 *  `message.usage`: input plus both cache counts), carried on the row the call
 *  wrote last as `content.usage` (`canopy_transcript.record_window`). */
export interface ContextUsage {
  context_tokens: number;
  /** The model id as the transcript records it (e.g. `claude-opus-5-5`). */
  model: string;
}

/** A send that landed on the server but hasn't reached the agent's turn yet
 *  — the server-derived queue, replacing whatever a client used to infer
 *  locally from its own optimistic sends. */
export interface QueuedMessage {
  turn_id: string;
  client_id: string;
  author: MessageAuthor | null;
  text: string;
  sent_at: string;
  /** `queued`: waiting for a runner. `delivering`: a runner took it as the
   *  next turn. `midturn`: typed into the agent's RUNNING turn, which reads it
   *  mid-turn (`Turn.rides_turn`). An older server never sends `midturn`. */
  state: "queued" | "delivering" | "midturn";
}

/** A send withdrawn (or stopped) before the agent read it. It never reaches the
 *  transcript, so the server lists it here and the client draws it where it was
 *  sent (`sent_at`), muted and marked cancelled — never at the tail, never live. */
export interface CancelledSend {
  turn_id: string;
  client_id: string;
  author: MessageAuthor | null;
  text: string;
  sent_at: string;
  /** Who withdrew it; "" when nobody recorded it. */
  cancelled_by: string;
  /** How its turn ended: `cancelled` (withdrawn or stopped), or `failed`, `lost`,
   *  `missed` or `done` for one that never reached the agent. Absent on an older
   *  server, which listed only cancelled sends. */
  state?: string;
  /** The runner's note on a send that was not delivered; "" otherwise. */
  detail?: string;
}

export interface Participant {
  user_id: number;
  email: string;
  display_name: string;
  role: "owner" | "editor" | "viewer";
  joined_at: string | null;
  last_seen_at: string | null;
}

/** A dialog an agent is blocked on.
 *
 *  `title` and `body` are what makes it answerable away from the keyboard:
 *  "Do you want to proceed?" tells you nothing without the command it means.
 *
 *  Two producers, one shape, so this never grows a second reader: the session
 *  report derives it from the transcript (an `AskUserQuestion` tool call — what
 *  actually blocks a fleet running `bypass permissions`), and the runner can
 *  still read it off the rendered screen for the dialogs a transcript cannot
 *  see. `source` says which, and a client is free to ignore it. */
/** One question of an `AskUserQuestion`. The TUI draws these as TABS and will
 *  not submit until each has an answer — so a surface that renders only the
 *  first one cannot complete the ask no matter which button is pressed. That
 *  was the bug: a two-question closeout showed one question, and a tap toggled
 *  a checkbox on a dialog that then sat waiting for a Submit nobody could
 *  reach (eva, 2026-08-12). */
export interface MenuQuestion {
  index: number;
  question: string;
  header?: string;
  /** Whether this question takes ANY number of answers. The TUI renders it as
   *  checkboxes and a number key TOGGLES one instead of answering, which is why
   *  a client that cannot see this flag renders the wrong control AND the
   *  runner presses the wrong key. */
  multi_select?: boolean;
  options: { number: number; label: string; description?: string }[];
}

export interface SessionMenu {
  question: string;
  title?: string;
  body?: string;
  selected?: number | null;
  source?: string;
  /** Every question in the ask, in declaration order. Absent on a dialog with
   *  no tool call behind it (a permission prompt, a trust gate) and on a menu
   *  from a producer older than this — in both cases the client falls back to
   *  the single-question fields above, which still describe question 1. */
  questions?: MenuQuestion[];
  /** `description` is present on the transcript path and is often the only
   *  thing that distinguishes two options — "Proceed to Phase 4" does not say
   *  that Phase 4 is test-gated, and its description does. */
  options: { number: number; label: string; description?: string }[];
  /** Set when a human's tap was RELAYED to the runner and then refused there —
   *  a stale dialog, a shell tab selected in emdash, an unreachable box. The
   *  API answers `ok:true` the moment it relays the frame, so without this a
   *  correct refusal is indistinguishable from a press that worked, and the
   *  button reads as dead. `answer_note` is the sentence to show; the code is
   *  for logs. The menu stays up alongside it, so there is something to retry. */
  answer_error?: string;
  answer_note?: string;
  /** Carried across a runner restart rather than observed this process. Nothing
   *  should branch on it — a tap verifies against the real screen either way. */
  restored?: boolean;
  /** Epoch seconds when a producer last SAW this dialog. The dialog lives on a
   *  terminal; this object is a copy, and without an age the only way to find
   *  out the copy is stale is to tap it and be refused. */
  observed_at?: number;
}

/**
 * Where the ask this session is waiting on currently stands.
 *
 * Computed on the SERVER (`apps/harness/turn_status.py`) and rendered
 * identically by every channel — a Slack thread, this chat kit, the embedded
 * widget. It exists because three of its states cannot be derived by a client
 * at all: its runner is offline, nothing is routed that could run it, the box
 * it was on died mid-turn. canopy's chat page used to guess at the first by
 * fetching the whole fleet and matching the runner BY NAME, which failed open
 * whenever a retired runner was omitted from that list; the widget never had
 * any of it and simply showed an empty panel.
 */
export interface TurnStatus {
  /** `picking_up` | `queued_behind` | `waiting_runner` | `signed_out` | `unrouted` | `working` | `blocked`
   *  | `paused` | `done` | `cancelled` | `missed` | `failed` | `lost`.
   *
   *  Deliberately a string rather than a union: an older client meeting a
   *  state a newer server added should fall back to its generic wording, not
   *  fail to type-check. `settled`/`stuck` are what code should branch on. */
  state: string;
  agent_slug: string | null;
  /** The runners this state is ABOUT — the ones that would pick it up while it
   *  is queued, or the one holding it. */
  runners: string[];
  claimed_by: string | null;
  /** Directed at this runner rather than offered to the cascade. */
  pinned: boolean;
  /** A runner it could be moved to right now, when moving it would help. Only
   *  ever an offer; acting on it re-checks everything server-side. */
  cloud_runner: string | null;
  cloud_runner_id: string | null;
  /** When the holding runner was last heard from. Only meaningful for
   *  `paused`, where it is what tells you whether to keep waiting. */
  last_seen_at: string | null;
  /** The agent is sitting on a dialog. A separate axis from `state`: a turn
   *  blocked on a question is still RUNNING as far as the turn is concerned. */
  menu_pending: boolean;
  /** For `queued_behind`: what it waits behind — `session` (the previous
   *  message in this conversation), `agent` (another turn of the same agent) or
   *  `transfer` (the session is moving to another runner). Absent on an older server. */
  behind?: "session" | "agent" | "transfer" | null;
  /** Why a `failed` turn failed, in the runner's words; or why a `cancelled`
   *  one was never run (an admission rule rejected it). Null otherwise, and
   *  for a turn someone stopped. Absent on an older server. */
  detail?: string | null;
  /** For `waiting_runner`: its runners are PAUSED by someone, not asleep.
   *  Absent on an older server. */
  parked?: boolean;
  /** For a `parked` turn: when the first of its runners resumes on its own
   *  (ISO). Null = only a person resuming one will run it. */
  held_until?: string | null;
  /** When a settled turn finished — lets the kit drop a failure the
   *  conversation has since moved past. Absent on an older server. */
  finished_at?: string | null;
  /** What the conversation's host requires of the runner (e.g. `sensitive_data`). Only
   *  set while queued, and only when the requirement is what blocks the turn
   *  (some runner would take it without one) — so render it as the reason.
   *  Absent on an older server. */
  requires?: string[];
  /** For `working`: when its runner took it, and when anything last came back
   *  from it (reply text, a tool call — or the claim itself). ISO. Absent on an
   *  older server. */
  claimed_at?: string | null;
  last_progress_at?: string | null;
  /** For `working`: a follow-up sent now is typed INTO the running turn (the
   *  agent reads it mid-turn) rather than waiting for it to end. Absent on an
   *  older server. */
  midturn?: boolean;
  /** For `working`, when it would not: why — `runner` (the box holding the
   *  turn does not deliver mid-turn, e.g. a cloud runner), `dialog` (the agent
   *  is on a question; it waits for the answer) or `confined` (a caller's
   *  turn runs in a session of its own). A string, for an older client. */
  midturn_blocked?: string | null;
  /** Nothing more will happen without someone asking for it. */
  settled: boolean;
  /** Nothing is moving and only a person can change that. The one question a
   *  "waiting on you" surface actually asks — derived once on the server so
   *  four clients cannot disagree about it. */
  stuck: boolean;
}

export interface SessionState {
  messages: Message[];
  /** Live agent activity, from the runner's turn-boundary hooks. Undefined when
   *  no hook has reported yet — the caller then falls back to the server's
   *  coarser `running` flag.
   *
   *  "blocked" means the agent wants a human: a permission prompt, or an idle
   *  wait for input. It is deliberately coarse — a hook observer cannot tell
   *  those apart — but it is the difference between "still thinking, wait" and
   *  "it is waiting on YOU", which previously rendered identically. */
  activity?: "working" | "idle" | "blocked";
  /** Whether a stop the human asked for actually landed. A SEPARATE axis from
   *  `activity`, deliberately: a stop that failed leaves the agent `working`,
   *  which is true and must stay true, so the outcome of the stop cannot be a
   *  value of activity without either lying or being lost.
   *
   *  "requested" is set the moment the server publishes to the runner — nothing
   *  has pressed Escape yet. Only the runner can say "stopped" or "failed", and
   *  it only says so after verifying the terminal (see #649: an unverified
   *  Escape reported as success is what made Stop untrustworthy). */
  stopState?: "requested" | "stopped" | "failed";
  /** The dialog the agent is waiting on. Carried in the CONNECT SNAPSHOT, not
   *  only in live frames: `session.activity` is view-only and reaches a client
   *  only if it was already connected when the agent blocked — which is exactly
   *  the case that fails, because you go and look BECAUSE it stopped. */
  menu?: SessionMenu;
  /** Where the ask stands. In the CONNECT SNAPSHOT as well as in live frames,
   *  for the same reason `menu` is: you open the page BECAUSE it went quiet, so
   *  the client that most needs this is the one that was not connected when it
   *  changed. Null/undefined when nothing has been asked on this session yet —
   *  which is distinct from "finished", and must stay distinct. */
  turn_status?: TurnStatus | null;
  /** The CALLER's own draft — null for a viewer, who has nothing to draft. */
  active_draft: Draft | null;
  /** Everyone else's live drafts, server-derived. Absent on an older server. */
  peer_drafts?: PeerDraft[];
  /** Sends that landed but haven't reached the agent's turn yet, server-
   *  derived and replaced wholesale on every frame — like `turn_status`, a
   *  client that just connected has no correct prior to merge a delta into.
   *  Absent on an older server. */
  queued?: QueuedMessage[];
  /** Sends withdrawn before the agent read them, oldest first, replaced
   *  wholesale like `queued`. Absent on an older server. */
  cancelled?: CancelledSend[];
  /** What the session's subagents are doing, one per `Agent` call (matched by
   *  `tool_use_id`), replaced wholesale. In the CONNECT SNAPSHOT for the same
   *  reason as `menu`: a background agent works while nobody is watching.
   *  Absent on an older server. */
  subagents?: SubagentSummary[];
  participants: Participant[];
  presence_user_ids: number[];
  /** The connecting user — null for a contact (a widget visitor), who has no
   *  user id. */
  current_user_id: number | null;
  /** The connecting CONTACT, when the viewer is one; null for a member.
   *  Absent on an older server. */
  current_contact_id?: number | null;
  /** The server holds rows older than the ones loaded here. In the snapshot,
   *  and kept by the reducer across reconnects and scroll-back loads (see
   *  `keepLoadedHistory`). Absent when nothing has said. */
  has_more_before?: boolean;
  /** The snapshot's oldest row's ordinal — where its tail begins. Absent on an
   *  older server; the reducer then uses the snapshot's lowest `turn_index`. */
  oldest_loaded_turn_index?: number | null;
}

/** One subagent of a session, from the files Claude Code keeps beside its
 *  transcript (`canopy_transcript.summarize_subagents`). `description` and
 *  `last` are blank on a sensitive conversation. */
export interface SubagentSummary {
  agent_id: string;
  /** The `Agent` tool call that started it — the row it belongs to. */
  tool_use_id: string;
  /** Set when another subagent started this one. */
  parent_agent_id: string;
  description: string;
  agent_type: string;
  /** Runs on after its tool call returned. */
  background: boolean;
  state: "running" | "done";
  /** Tool calls it has made. */
  steps: number;
  /** Its latest tool call, as `Name · target`. */
  last: string;
  started_at: string | null;
  updated_at: string | null;
}

// ---------------------------------------------------------------------------
// WebSocket protocol
// ---------------------------------------------------------------------------

export type WsAction =
  | { action: "chat.send"; data: Record<string, never> }
  | { action: "chat.stop"; data: { message_id: string } }
  /** Withdraw one of your own sends while it is still QUEUED (not yet claimed
   *  by a runner). Refused with `session.error` `unqueue_refused` otherwise. */
  | { action: "chat.unqueue"; data: { turn_id: string } }
  | { action: "draft.update"; data: { version: number; body: string } }
  // The mode is its OWN idempotent frame, not a field on the version-guarded
  // `draft.update` above — applied unconditionally server-side (no version
  // check), so it can never race a keystroke and never itself causes a
  // `draft_version_mismatch`. A `draft.update` carrying a stray `visibility`
  // (an in-flight older 0.14 client) is simply ignored server-side.
  | { action: "draft.set_visibility"; data: { visibility: TypingVisibility } }
  /** @deprecated accepted and ignored by the server since 0.13 — every editor
   *  gets their own draft now, so there is no lock left to take over. */
  | { action: "draft.take_over"; data: Record<string, never> }
  | { action: "draft.discard"; data: Record<string, never> }
  | { action: "presence.heartbeat"; data: Record<string, never> };

export type WsEvent =
  | { event: "session.state"; data: SessionState }
  | { event: "session.error"; data: { code: string; message: string; detail?: unknown } }
  | { event: "session.title_updated"; data: { title: string } }
  | { event: "chat.stream_start"; data: { message_id: string; turn_index: number } }
  // The agent started or finished a turn. Distinct from tool events: it fires
  // while Claude is THINKING, before any content exists to show.
  | { event: "session.activity"; data: { state: "working" | "idle" | "blocked"; menu?: SessionMenu } }
  | { event: "session.stop"; data: { state: "requested" | "stopped" | "failed" } }
  // The agent started, or stopped, waiting on a dialog. Its own frame rather
  // than an overloaded `session.activity`: activity answers "is it producing",
  // which the hook path owns on a much faster clock, and inventing a state here
  // to carry a menu would report an agent as idle or blocked on the wrong one.
  // `menu: null` is the retraction — somebody answered at the keyboard.
  | { event: "session.menu"; data: { menu: SessionMenu | null } }
  // The session's subagents changed — the whole set, replaced wholesale.
  | { event: "session.subagents"; data: { subagents: SubagentSummary[] } }
  // Where the ask stands, re-derived server-side on every transition. Its own
  // frame rather than a field on `session.activity`: activity is the runner's
  // fast hook clock reporting whether the agent is PRODUCING, while this is the
  // turn's placement in the fleet — they change on different clocks and for
  // different reasons, and the states that matter most here (queued behind an
  // offline box, unroutable) are ones activity has no way to spell.
  | { event: "session.turn_status"; data: { status: TurnStatus | null } }
  // A human typed into emdash rather than into this page. No client echoed it,
  // so this is the only way it reaches the browser before a reload. `author`
  // rides here for the same reason it rides the queued list (spec 2026-09-26):
  // a transcript-sourced session's durable row carries no user beyond the
  // runner's own login, so this is canopy's only chance to say who typed it.
  // `client_id` is likewise the only way an HTTP send's OPTIMISTIC row (built
  // locally by `noteLocalSend`, never a real server frame) can carry the same
  // id its REST body sent — without it `QueuedRows.hideClientIds` cannot match
  // this row to the server's queued-turn projection, and a contact's or
  // widget's own send rendered twice: once as their bubble, once as a "queued"
  // placeholder for the same send.
  | { event: "chat.user_message"; data: { message_id: string; turn_index: number; plaintext: string; author?: MessageAuthor | null; client_id?: string; record?: Record<string, unknown> } }
  | { event: "chat.delta"; data: { message_id: string; text: string } }
  // `turn_index` is the row's transcript ordinal — the same key the persisted
  // Message carries, so a live tool row sorts into exactly the position it will
  // occupy after a reload. Optional: an older server omits it.
  | { event: "chat.tool_use"; data: { parent_message_id: string | null; tool_message_id: string; turn_index?: number; block: Record<string, unknown> } }
  | { event: "chat.tool_result"; data: { parent_message_id: string | null; tool_message_id: string; turn_index?: number; block: Record<string, unknown>; app?: MessageAppRef | null; secret_request?: MessageSecretRequestRef | null } }
  | {
      event: "chat.stream_complete";
      /** `api_error`: the row is Claude Code's own error notice, not a reply;
       *  `signed_out`: that notice says the box's Claude login is gone. */
      data: {
        message_id: string; plaintext: string; api_error?: string; signed_out?: boolean;
        /** How full the context window was for this model call. */
        usage?: ContextUsage;
      };
    }
  | { event: "chat.stream_error"; data: { message_id: string; detail: string } }
  | { event: "chat.stream_cancelled"; data: { message_id: string | null; partial_len: number } }
  | { event: "draft.updated"; data: Draft }
  // Someone else's box, live — keyed by author, not draft id, because a peer's
  // draft is never addressable by this client and never needs to be: only
  // "what are they typing right now" does. An empty `body` means they sent,
  // discarded, or cleared it. Replaces `draft.lock_changed`, which existed
  // only because a session had one shared next-draft to fight over; now
  // everyone has their own, so there is no lock left to report.
  | { event: "draft.typing"; data: PeerDraft }
  // `client_id` is the sender's receipt for a send that carried one (see
  // useSessionSocket.sendChat); absent from older servers.
  | { event: "draft.committed"; data: { draft_id: string; user_message_id: string; client_id?: string } }
  | { event: "draft.discarded"; data: { draft_id: string } }
  // `participant` carries WHO joined, so a client can add them to its
  // participant list. Without it a first-time joiner has an id and no name,
  // and the presence row (which renders participants filtered by presence)
  // cannot show them at all. Optional so an older server degrades rather than
  // breaks. The bare `email`/`display_name` below are the vestigial shape that
  // was declared but never sent by anything.
  | {
      event: "presence.joined";
      data: {
        user_id: number;
        participant?: Participant;
        email?: string;
        display_name?: string;
      };
    }
  | { event: "presence.left"; data: { user_id: number } }
  // The whole queue, wholesale, like `session.turn_status` — a client that
  // just connected has no correct prior to merge a delta into.
  | { event: "session.queued"; data: { queued: QueuedMessage[]; cancelled?: CancelledSend[] } };
