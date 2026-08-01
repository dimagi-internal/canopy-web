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
}

export interface Draft {
  /** String PK (canopy sends `str(draft.pk)`). */
  id: string;
  slot: "next" | "queued";
  status: "open" | "sent" | "discarded";
  body: string;
  version: number;
  last_editor: number;
  last_edit_at: string;
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
export interface SessionMenu {
  question: string;
  title?: string;
  body?: string;
  selected?: number | null;
  source?: string;
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
  /** The dialog the agent is waiting on. Carried in the CONNECT SNAPSHOT, not
   *  only in live frames: `session.activity` is view-only and reaches a client
   *  only if it was already connected when the agent blocked — which is exactly
   *  the case that fails, because you go and look BECAUSE it stopped. */
  menu?: SessionMenu;
  active_draft: Draft | null;
  participants: Participant[];
  presence_user_ids: number[];
  current_user_id: number;
}

// ---------------------------------------------------------------------------
// WebSocket protocol
// ---------------------------------------------------------------------------

export type WsAction =
  | { action: "chat.send"; data: Record<string, never> }
  | { action: "chat.stop"; data: { message_id: string } }
  | { action: "draft.update"; data: { version: number; body: string } }
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
  // The agent started, or stopped, waiting on a dialog. Its own frame rather
  // than an overloaded `session.activity`: activity answers "is it producing",
  // which the hook path owns on a much faster clock, and inventing a state here
  // to carry a menu would report an agent as idle or blocked on the wrong one.
  // `menu: null` is the retraction — somebody answered at the keyboard.
  | { event: "session.menu"; data: { menu: SessionMenu | null } }
  // A human typed into emdash rather than into this page. No client echoed it,
  // so this is the only way it reaches the browser before a reload.
  | { event: "chat.user_message"; data: { message_id: string; turn_index: number; plaintext: string } }
  | { event: "chat.delta"; data: { message_id: string; text: string } }
  // `turn_index` is the row's transcript ordinal — the same key the persisted
  // Message carries, so a live tool row sorts into exactly the position it will
  // occupy after a reload. Optional: an older server omits it.
  | { event: "chat.tool_use"; data: { parent_message_id: string | null; tool_message_id: string; turn_index?: number; block: Record<string, unknown> } }
  | { event: "chat.tool_result"; data: { parent_message_id: string | null; tool_message_id: string; turn_index?: number; block: Record<string, unknown> } }
  | { event: "chat.stream_complete"; data: { message_id: string; plaintext: string } }
  | { event: "chat.stream_error"; data: { message_id: string; detail: string } }
  | { event: "chat.stream_cancelled"; data: { message_id: string | null; partial_len: number } }
  | { event: "draft.updated"; data: Draft }
  | { event: "draft.lock_changed"; data: { draft_id: string; holder_user_id: number | null; expires_at: number | null } }
  | { event: "draft.committed"; data: { draft_id: string; user_message_id: string } }
  | { event: "draft.discarded"; data: { draft_id: string } }
  | { event: "presence.joined"; data: { user_id: number; email?: string; display_name?: string } }
  | { event: "presence.left"; data: { user_id: number } };
