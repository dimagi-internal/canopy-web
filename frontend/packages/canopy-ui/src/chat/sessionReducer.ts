import type { Draft, Message, SessionState, WsEvent } from "./protocol";

// Pure reducer for SessionState — extracted from useSessionSocket so it
// can be unit-tested without WebSocket plumbing. Side-effect events
// (session.title_updated → optional injected callback; session.error →
// setLastError + clear draft debounce) stay in the hook itself.
//
// Keep this file dependency-free (no React) so a vitest run doesn't pull
// jsdom or RTL.
//
// canopy adaptation vs ace: message/draft ids are STRINGS, and
// `chat.stream_start` UPSERTS the assistant message — canopy's
// `draft.committed` carries only `user_message_id` (no assistant id to
// pre-insert), so the assistant row is created lazily when its first stream
// frame arrives.
export function sessionReducer(prev: SessionState, frame: WsEvent): SessionState {
  switch (frame.event) {
    case "session.state":
      return frame.data;

    case "chat.stream_start": {
      // Upsert: if the assistant message already exists (rare — a runner that
      // pre-inserts it), flip it to streaming; otherwise create it. canopy's
      // draft.committed cannot pre-send the assistant id, so this is the
      // normal path for making the streamed reply visible.
      const exists = prev.messages.some((m) => m.id === frame.data.message_id);
      if (exists) {
        return {
          ...prev,
          messages: prev.messages.map((m) =>
            m.id === frame.data.message_id
              ? { ...m, status: "streaming" as const }
              : m,
          ),
        };
      }
      const nowIso = new Date().toISOString();
      const assistant: Message = {
        id: frame.data.message_id,
        turn_index: frame.data.turn_index,
        role: "assistant",
        content: {},
        plaintext: "",
        status: "streaming",
        error_detail: null,
        started_at: nowIso,
        completed_at: null,
        created_at: nowIso,
      };
      return { ...prev, messages: [...prev.messages, assistant] };
    }

    case "chat.delta":
      return {
        ...prev,
        messages: prev.messages.map((m) =>
          m.id === frame.data.message_id
            ? { ...m, plaintext: m.plaintext + frame.data.text }
            : m,
        ),
      };

    case "chat.stream_complete":
      return {
        ...prev,
        messages: prev.messages.map((m) =>
          m.id === frame.data.message_id
            ? {
                ...m,
                plaintext: frame.data.plaintext,
                status: "complete" as const,
              }
            : m,
        ),
      };

    case "chat.stream_error":
      // NOTE: backend emits chat.stream_error with detail="cancelled"
      // for stop-driven cancellation; there's no separate
      // chat.stream_cancelled event in practice. Distinguished by detail.
      return {
        ...prev,
        messages: prev.messages.map((m) =>
          m.id === frame.data.message_id
            ? {
                ...m,
                status: "error" as const,
                error_detail: frame.data.detail,
              }
            : m,
        ),
      };

    case "chat.stream_cancelled":
      return {
        ...prev,
        messages: prev.messages.map((m) =>
          m.id === frame.data.message_id
            ? {
                ...m,
                status: "error" as const,
                error_detail: `cancelled (partial: ${frame.data.partial_len} chars)`,
              }
            : m,
        ),
      };

    case "chat.tool_use":
    case "chat.tool_result": {
      // Tool rows are real Message rows on the server; these frames are the
      // same row arriving live. Upsert by id so a re-delivery (reconnect
      // catch-up, a retried post) updates in place instead of doubling the
      // row — and so a tool_result that lands twice can't orphan its pair.
      const role = frame.event === "chat.tool_use" ? "tool_use" : "tool_result";
      const block = frame.data.block ?? {};
      const plaintext = typeof block.text === "string" ? block.text : "";
      const id = frame.data.tool_message_id;
      const existing = prev.messages.find((m) => m.id === id);
      if (existing) {
        return {
          ...prev,
          messages: prev.messages.map((m) =>
            m.id === id ? { ...m, content: block, plaintext } : m,
          ),
        };
      }
      const nowIso = new Date().toISOString();
      const message: Message = {
        id,
        // Fall back to appending after the newest row when the server didn't
        // send an ordinal — order still holds, since frames arrive in order.
        turn_index:
          frame.data.turn_index ??
          (prev.messages[prev.messages.length - 1]?.turn_index ?? 0) + 1,
        role,
        content: block,
        plaintext,
        status: block.is_error === true ? "error" : "complete",
        error_detail: null,
        started_at: nowIso,
        completed_at: nowIso,
        created_at: nowIso,
      };
      return { ...prev, messages: [...prev.messages, message] };
    }

    case "draft.updated": {
      const incoming = frame.data as Draft;
      // If we're the current editor, keep our local body — the server
      // echo is stale relative to keystrokes that happened since the
      // debounced send. Only accept metadata (version, last_editor, etc).
      if (
        prev.active_draft &&
        incoming.last_editor === prev.current_user_id
      ) {
        return {
          ...prev,
          active_draft: {
            ...prev.active_draft,
            version: incoming.version,
            last_editor: incoming.last_editor,
            last_edit_at: incoming.last_edit_at,
          },
        };
      }
      return { ...prev, active_draft: incoming };
    }

    case "draft.lock_changed":
      if (prev.active_draft && prev.active_draft.id === frame.data.draft_id) {
        return {
          ...prev,
          active_draft: {
            ...prev.active_draft,
            last_editor: frame.data.holder_user_id ?? prev.active_draft.last_editor,
          },
        };
      }
      return prev;

    case "draft.committed": {
      // Insert the optimistic USER message from the draft body that's about
      // to be cleared. The assistant reply is NOT inserted here — canopy's
      // draft.committed carries no assistant id; `chat.stream_start` upserts
      // that row when the reply begins.
      //
      // Also clear active_draft.body here. The server creates a new empty
      // draft with last_editor=sender, so the follow-up draft.updated hits
      // the "keep local body" branch above and would otherwise leave the
      // just-sent text in the textarea — which lets Enter re-send the same
      // turn repeatedly.
      const prevDraftBody = prev.active_draft?.body ?? "";
      const maxTurnIndex = prev.messages.reduce(
        (acc, msg) => Math.max(acc, msg.turn_index),
        0,
      );
      const nowIso = new Date().toISOString();
      const userMessage: Message = {
        id: frame.data.user_message_id,
        turn_index: maxTurnIndex + 1,
        role: "user",
        content: { text: prevDraftBody },
        plaintext: prevDraftBody,
        status: "complete",
        error_detail: null,
        started_at: null,
        completed_at: nowIso,
        created_at: nowIso,
      };
      return {
        ...prev,
        active_draft: prev.active_draft
          ? { ...prev.active_draft, body: "" }
          : prev.active_draft,
        messages: [...prev.messages, userMessage],
      };
    }

    case "draft.discarded":
      if (prev.active_draft && prev.active_draft.id === frame.data.draft_id) {
        return {
          ...prev,
          active_draft: { ...prev.active_draft, body: "" },
        };
      }
      return prev;

    case "presence.joined": {
      const ids = new Set(prev.presence_user_ids);
      ids.add(frame.data.user_id);
      return { ...prev, presence_user_ids: [...ids] };
    }

    case "presence.left":
      return {
        ...prev,
        presence_user_ids: prev.presence_user_ids.filter(
          (id) => id !== frame.data.user_id,
        ),
      };

    case "session.error": {
      // Side effects (setLastError, clear draft debounce) are handled
      // by the hook; the reducer only knows about the version-mismatch
      // recovery, which mutates active_draft.
      if (
        frame.data.code === "draft_version_mismatch" &&
        frame.data.detail &&
        typeof frame.data.detail === "object"
      ) {
        const detail = frame.data.detail as {
          current_version: number;
          current_body: string;
        };
        return prev.active_draft
          ? {
              ...prev,
              active_draft: {
                ...prev.active_draft,
                version: detail.current_version,
                body: detail.current_body,
              },
            }
          : prev;
      }
      return prev;
    }

    case "session.title_updated":
      // Pure reducer leaves this alone — the hook calls its optional
      // onTitleUpdated callback on receipt and short-circuits. Included
      // here so an exhaustive switch type-checks.
      return prev;

    default:
      return prev;
  }
}
