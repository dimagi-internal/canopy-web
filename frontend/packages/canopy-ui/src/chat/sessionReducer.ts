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
/** Frames that prove the agent is producing again.
 *
 *  `Notification` fires on the way IN to a wait and NOTHING fires on the way
 *  out — approving a permission prompt emits no hook at all. So without this,
 *  a session shows "needs you" for the rest of the turn, long after you
 *  answered. Any row the agent emits afterwards is the proof the hook cannot
 *  give. */
const UNBLOCKING_FRAMES = new Set([
  "chat.stream_start",
  "chat.delta",
  "chat.tool_use",
  "chat.tool_result",
]);

/**
 * Every frame `sessionReducer` handles. The socket hook hands the reducer ONLY
 * these, and gives anything else to `onUnknownEvent`, so a frame missing from
 * here is a frame the reducer never sees. That happened: from #775
 * (2026-09-13) the hook kept its own hand-written list, which lacked every
 * `draft.*` and `presence.*` frame and `session.turn_status`. Your own line did
 * not appear when you sent it (`draft.committed`), and the co-edited draft,
 * the presence roster and the live turn status all went quiet, with no error.
 * `sessionReducer.events.test.ts` fails if this and the switch below differ.
 */
export const REDUCER_EVENTS: ReadonlySet<string> = new Set([
  "session.state", "session.activity", "session.stop", "session.menu",
  "session.turn_status", "session.error", "session.title_updated",
  "chat.stream_start", "chat.user_message", "chat.delta", "chat.stream_complete",
  "chat.stream_error", "chat.stream_cancelled", "chat.tool_use", "chat.tool_result",
  "draft.updated", "draft.lock_changed", "draft.committed", "draft.discarded",
  "presence.joined", "presence.left",
]);

export function sessionReducer(prev: SessionState, frame: WsEvent): SessionState {
  if (frame.event === "chat.stream_start" || frame.event === "draft.committed") {
    // A new turn is starting, so the previous turn's stop outcome is history —
    // leaving "your stop did not take" pinned over fresh work would be a stale
    // warning about something the human has already moved on from.
    prev = { ...prev, stopState: undefined };
  }
  if (prev.activity === "blocked" && UNBLOCKING_FRAMES.has(frame.event)) {
    // Dropping the menu with the state matters as much as the state itself —
    // the dialog is gone, and buttons that answer a gone dialog send a stray
    // keystroke into the prompt.
    prev = { ...prev, activity: "working", menu: undefined };
  }
  switch (frame.event) {
    case "session.state": {
      const messages = keepUnechoedSends(prev.messages, frame.data.messages);
      return messages === frame.data.messages ? frame.data : { ...frame.data, messages };
    }

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

    case "session.activity":
      // A frame that says the agent is NOT waiting retracts the menu — a stale
      // dialog is worse than none, because its buttons would answer a prompt
      // that is no longer on screen.
      //
      // A `blocked` frame that carries no menu does NOT, and that asymmetry is
      // load-bearing. The hook path reports `blocked` without one on purpose
      // (#510 — reading the screen stole emdash's focus), so treating a bare
      // `blocked` as a retraction would erase every menu the snapshot and the
      // session report supply, which is now all of them.
      if (frame.data.state !== "blocked") {
        return { ...prev, activity: frame.data.state, menu: undefined };
      }
      return { ...prev, activity: "blocked", menu: frame.data.menu ?? prev.menu };

    case "session.stop":
      // Independent of `activity` on purpose. A stop that FAILED leaves the agent
      // working, and both facts have to be sayable at once: "it is still going"
      // AND "your stop did not take". Collapsing them loses whichever one loses
      // the race, and it was always the second — which is how a dead Stop button
      // stayed invisible.
      return { ...prev, stopState: frame.data.state };

    case "session.menu":
      // The authoritative producer: the session report re-derives the dialog
      // from the transcript every ~10s and pushes only the edges. `null` is the
      // retraction, and has to be honoured — somebody answered at the keyboard.
      return { ...prev, menu: frame.data.menu ?? undefined };

    case "session.turn_status":
      // Wholesale, never merged. The server re-derives the whole status from
      // the turn on every transition, so a partial update could only ever
      // describe a state the server does not believe in — and a client that
      // just connected has no correct prior to merge INTO. Same argument as
      // the ACP rule in reverse: `tool_call_update` is a patch because it is
      // specified as one; this is not.
      //
      // A null status is a real answer ("nothing has been asked here"), not a
      // gap to preserve, so it is stored as such.
      return { ...prev, turn_status: frame.data.status ?? null };

    case "chat.user_message": {
      // Someone typed into emdash, OR into this page. Both reach here, and that
      // is why matching on turn_index alone is not enough: a web send writes its
      // row at a DENSE index (services._next_index), then the agent reads the
      // message and the transcript re-ships the same text at a COMPOSITE ordinal
      // (record * BLOCK_STRIDE + block). Same words, two different indices, two
      // different ids — so the upsert missed and the message rendered twice
      // live, while a reload showed it once (get_or_create dedupes server-side).
      //
      // Falling back to matching identical text on a recent user row closes it.
      // Deliberately narrow: same role, same text, and only against the tail, so
      // a genuine repeat of a short message ("yes") sent much later still lands
      // as its own row.
      const RECENT_USER_ROWS = 6;
      const sameText = (m: Message) =>
        m.role === "user" &&
        m.plaintext.trim() !== "" &&
        m.plaintext.trim() === frame.data.plaintext.trim();
      const recentUsers = prev.messages.filter((m) => m.role === "user").slice(-RECENT_USER_ROWS);
      const existing =
        prev.messages.find(
          (m) => m.id === frame.data.message_id ||
            (m.role === "user" && m.turn_index === frame.data.turn_index),
        ) ?? recentUsers.find(sameText);
      if (existing) {
        return {
          ...prev,
          messages: prev.messages.map((m) =>
            m === existing
              ? {
                  ...m,
                  id: frame.data.message_id,
                  // Take the incoming ordinal: the transcript's composite index
                  // is the durable one, so the row sorts where a reload puts it.
                  turn_index: frame.data.turn_index,
                  plaintext: frame.data.plaintext,
                  // The agent read it, so a pending or unconfirmed row is sent.
                  ...(m.role === "user" ? { status: "complete" as const, error_detail: null } : {}),
                }
              : m,
          ),
        };
      }
      const nowIso = new Date().toISOString();
      const user: Message = {
        id: frame.data.message_id,
        turn_index: frame.data.turn_index,
        role: "user",
        content: { text: frame.data.plaintext },
        plaintext: frame.data.plaintext,
        status: "complete",
        error_detail: null,
        started_at: null,
        completed_at: null,
        created_at: nowIso,
      };
      return { ...prev, messages: [...prev.messages, user] };
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
      // Reconcile on the CORRELATION key, not the message id. The same tool call
      // arrives twice by design: once live from a hook (no ordinal, id `seq:-1`)
      // and once durably from the transcript (ordinal-keyed, a real id). They
      // share only `tool_use_id` / `id` in content, so matching on that is what
      // makes the live row a placeholder the durable row REPLACES rather than a
      // duplicate that sits beside it forever.
      const correlation =
        role === "tool_use"
          ? (block.id as string | undefined)
          : (block.tool_use_id as string | undefined);
      const existing = prev.messages.find(
        (m) =>
          m.id === id ||
          (m.role === role &&
            correlation !== undefined &&
            correlation !== "" &&
            (m.content as Record<string, unknown>)?.[
              role === "tool_use" ? "id" : "tool_use_id"
            ] === correlation),
      );
      if (existing) {
        // Adopt the incoming id and ordinal: a durable row superseding a live
        // placeholder must take over its identity, or the next update keys on a
        // `seq:-1` that no longer means anything.
        return {
          ...prev,
          messages: prev.messages.map((m) =>
            m === existing
              ? {
                  ...m,
                  id,
                  turn_index: frame.data.turn_index ?? m.turn_index,
                  content: block,
                  plaintext,
                }
              : m,
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
      // The sender already shows its line (a pending row stamped with this
      // client_id, see `withPendingSend`). Confirm it rather than add a second.
      const cid = frame.data.client_id;
      if (cid && prev.messages.some((m) => m.content?.client_id === cid)) {
        return {
          ...markSendDelivered(prev, cid, frame.data.user_message_id),
          active_draft: prev.active_draft ? { ...prev.active_draft, body: "" } : prev.active_draft,
        };
      }
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
      // Adopt the joiner into `participants` too. The presence ROW renders
      // participants filtered by presence, so an id with no matching
      // participant is invisible — which is what made a first-time joiner
      // unseeable by everyone already in the room until they reloaded.
      // `participant` is optional (an older server may not send it); without
      // it this degrades to exactly the previous behaviour rather than
      // inventing a nameless entry.
      const joined = frame.data.participant;
      const known = joined
        ? prev.participants.some((p) => p.user_id === joined.user_id)
        : true;
      return {
        ...prev,
        presence_user_ids: [...ids],
        participants:
          joined && !known ? [...prev.participants, joined] : prev.participants,
      };
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


// ---------------------------------------------------------------------------
// A send the SENDER is waiting to hear back about.
//
// Your line goes on screen the moment you press send, as "pending", stamped
// with the send's client_id. It becomes "complete" on the server's receipt
// (`draft.committed` carrying that id), or when an HTTP resend succeeds, or when
// the transcript echoes it back. If none of those happen it becomes "error",
// and the words stay on screen. Before this the composer cleared on send and
// nothing showed the line until the agent read it, so a send lost on a dead
// socket simply vanished (2026-09-23).
// ---------------------------------------------------------------------------

export function withPendingSend(
  prev: SessionState,
  { clientId, text }: { clientId: string; text: string },
): SessionState {
  const nowIso = new Date().toISOString();
  const row: Message = {
    id: `local:${clientId}`,
    turn_index: prev.messages.reduce((acc, m) => Math.max(acc, m.turn_index), 0) + 1,
    role: "user",
    content: { text, client_id: clientId },
    plaintext: text,
    status: "pending",
    error_detail: null,
    started_at: null,
    completed_at: null,
    created_at: nowIso,
  };
  return { ...prev, messages: [...prev.messages, row] };
}

function updateSend(prev: SessionState, clientId: string, patch: Partial<Message>): SessionState {
  let changed = false;
  const messages = prev.messages.map((m) => {
    if (m.content?.client_id !== clientId) return m;
    changed = true;
    return { ...m, ...patch };
  });
  return changed ? { ...prev, messages } : prev;
}

export function markSendDelivered(prev: SessionState, clientId: string, messageId?: string): SessionState {
  return updateSend(prev, clientId, {
    status: "complete",
    error_detail: null,
    completed_at: new Date().toISOString(),
    ...(messageId ? { id: messageId } : {}),
  });
}

export function markSendFailed(prev: SessionState, clientId: string, detail: string): SessionState {
  // Only a row still waiting can fail: a late failure must not overturn a
  // receipt or a transcript echo that already proved the send landed.
  const row = prev.messages.find((m) => m.content?.client_id === clientId);
  if (!row || row.status !== "pending") return prev;
  return updateSend(prev, clientId, { status: "error", error_detail: detail });
}

/** Your own sends the snapshot does not hold yet, carried across it.
 *
 *  A snapshot REPLACES the message list, and in a transcript-sourced session
 *  your line is not a server row until the agent has read it. That can be
 *  minutes if a turn is still running, since a session runs one turn at a
 *  time. So every reconnect in that window (a phone resuming, a flaky network)
 *  took the line off screen, and it came back only when the agent read it. It
 *  looked lost. A row is kept only while the snapshot lacks it: once the
 *  transcript echo is in the snapshot (same words among its recent user rows),
 *  the snapshot's copy wins.
 */
function keepUnechoedSends(previous: Message[], snapshot: Message[]): Message[] {
  const mine = previous.filter(
    (m) => m.role === "user" && typeof m.content?.client_id === "string" &&
      !snapshot.some((s) => s.id === m.id),
  );
  if (mine.length === 0) return snapshot;
  const recent = new Set(
    snapshot.filter((m) => m.role === "user").slice(-12).map((m) => m.plaintext.trim()),
  );
  const carried = mine.filter((m) => !recent.has(m.plaintext.trim()));
  if (carried.length === 0) return snapshot;
  const top = snapshot.reduce((acc, m) => Math.max(acc, m.turn_index), 0);
  return [...snapshot, ...carried.map((m, i) => ({ ...m, turn_index: top + 1 + i }))];
}
