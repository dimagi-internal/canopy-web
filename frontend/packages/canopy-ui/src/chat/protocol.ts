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

export interface SessionState {
  messages: Message[];
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
