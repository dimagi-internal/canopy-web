import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { fromAgui, resetAguiState } from "./agui";
import type { Draft, Message, SessionState, WsEvent } from "./protocol";
import { shouldSyncDraftLive } from "./drafts";
import { prependHistory } from "./history";
import { sessionReducer } from "./sessionReducer";

const HEARTBEAT_INTERVAL_MS = 20_000;
const RECONNECT_DELAYS_MS = [1_000, 2_000, 5_000, 10_000];
const DRAFT_UPDATE_DEBOUNCE_MS = 150;

const INITIAL_STATE: SessionState = {
  messages: [],
  active_draft: null,
  participants: [],
  presence_user_ids: [],
  current_user_id: 0,
};

export interface UseSessionSocketOptions {
  /** The chat session id (UUID string). */
  sessionId: string;
  /**
   * App-injected WebSocket URL builder. The kit never imports app routing/base
   * helpers; the container passes one (e.g. canopy's `wsUrl`). Called with the
   * relative path `ws/canopy-sessions/${sessionId}/`.
   */
  wsUrl: (path: string) => string;
  /**
   * Optional side-effect callback fired when the server broadcasts a
   * `session.title_updated` (replaces ace's `notifySessionsUpdated`). The kit
   * has no opinion on what to do with it.
   */
  onTitleUpdated?: () => void;
  /**
   * A frame the kit does not understand.
   *
   * The kit stays agnostic: canopy grew `session.page_action` (an agent asking
   * the embedded page to do something) and ace-web will grow its own. Teaching
   * the reducer about each would make a shared kit carry one app's vocabulary.
   * Same shape as `onTitleUpdated` — handed over, no opinion taken.
   */
  onUnknownEvent?: (frame: WsEvent) => void;
  /**
   * Which vocabulary to ask the server for.
   *
   * `"canopy"` (the default) is this kit's own frames, unchanged — the reason
   * an existing consumer, including ace-web installing `canopy-ui` from npm,
   * notices nothing. `"ag-ui"` asks the server to project the same conversation
   * into AG-UI and translates it back here, so the reducer never learns a
   * second vocabulary and there is one behaviour to test rather than two.
   *
   * Opting in buys interoperability, not features: a canopy frame and its
   * AG-UI projection reduce to identical state (`agui.test.ts` asserts exactly
   * that against a fixture the server generates).
   */
  protocol?: "canopy" | "ag-ui";
  /**
   * For a principal whose socket is READ-ONLY — a canopy CONTACT, someone a
   * site vouched for who has no canopy account. They can watch the conversation
   * over the socket but not write to it, so their message goes out over HTTP
   * (canopy-client's `rest.send`, which routes to `/api/contact/…`) and their
   * draft stays local: there is no shared draft to co-edit with nobody else.
   *
   * Everything else is unchanged — the same `state`, the same pending row, the
   * same "waiting for a reply" — so one chat UI serves a user and a contact
   * without a second send path in every host. Omit for a user (the default).
   */
  sendOverHttp?: (text: string) => Promise<unknown>;
}

/**
 * Ask for AG-UI on the URL the caller built — whatever it looks like.
 *
 * The hook OWNS this flag rather than handing the caller a path with it already
 * attached, because that contract was invisible and two of the first three
 * callers broke it: canopy-web's widget builds `client.sessionSocketUrl(id)` and
 * ace-web builds `buildCanopyWsUrl(base, id)`, and both ignore the path they are
 * given (they need their own token in the query). The flag was dropped, the
 * server answered in native frames, and every one of them decoded as AG-UI to
 * nothing — a blank chat with no error, caught only in review before it shipped.
 *
 * Exported for the test, which asserts on the URL a socket really opens.
 */
export function withAguiProtocol(url: string): string {
  // Empty is a caller saying "no URL yet" (the widget before it has a
  // session); decorating it would turn a deliberate no-op into a bad request.
  if (!url) return url;
  if (/[?&]protocol=/.test(url)) return url;
  return `${url}${url.includes("?") ? "&" : "?"}protocol=ag-ui`;
}

export interface UseSessionSocketResult {
  state: SessionState;
  connected: boolean;
  /** A send is outstanding with no reply yet — the turn is QUEUED, waiting for
   *  a runner. Nothing in `state` can express this (there is no assistant
   *  message until the first token), and it is what keeps Stop reachable while
   *  a turn is stuck. */
  awaitingReply: boolean;
  sendChat: () => void;
  stopChat: (messageId: string | null) => void;
  updateDraft: (body: string) => void;
  takeOverDraft: () => void;
  discardDraft: () => void;
  prependMessages: (older: Message[]) => void;
  /** A message was sent for this session over HTTP rather than through this
   *  socket — show it, and start waiting for a reply.
   *
   *  Two callers need it and both are REST sends: the embedded widget's FIRST
   *  message (the session does not exist until it is sent, so there is no
   *  socket to send it on) and every message from a CONTACT (whose socket
   *  listens only — presence and the co-edited draft are keyed on a user id
   *  they do not have).
   *
   *  Without it those sends changed nothing on screen: `awaitingReply` is set
   *  by `sendChat` alone, and the user's own line is not a server row until
   *  the agent's transcript ships it back. So you typed, pressed send, and got
   *  an empty panel — for as long as the reply took, and forever if its runner
   *  was offline. */
  noteLocalSend: (text: string) => void;
  lastError: string | null;
}

/**
 * Frames `sessionReducer` understands. Anything else is handed to
 * `onUnknownEvent` rather than dropped — the reducer ignores what it does not
 * recognise, which silently swallows an app-specific frame and leaves the
 * container wondering why its feature never fires.
 *
 * Keep in step with sessionReducer's own switch.
 */
const KNOWN_EVENTS = new Set([
  "chat.delta", "chat.stream_cancelled", "chat.stream_complete",
  "chat.stream_error", "chat.stream_start", "chat.tool_result",
  "chat.tool_use", "chat.user_message", "session.activity", "session.error",
  "session.menu", "session.state", "session.stop", "session.title_updated",
]);

export function useSessionSocket({
  sessionId,
  wsUrl,
  onTitleUpdated,
  onUnknownEvent,
  protocol = "canopy",
  sendOverHttp,
}: UseSessionSocketOptions): UseSessionSocketResult {
  // The local draft a read-only principal types into (see `sendOverHttp`).
  const [localDraft, setLocalDraft] = useState<Draft | null>(null);
  const [state, setState] = useState<SessionState>(INITIAL_STATE);
  const [connected, setConnected] = useState(false);
  const [lastError, setLastError] = useState<string | null>(null);
  // A send has gone out but no reply has begun — i.e. the turn is QUEUED,
  // waiting for a runner to claim it. There is no assistant message during
  // this window, so nothing else in the state can express it, and without it
  // the Stop control is unreachable exactly when the turn is stuck.
  const [awaitingReply, setAwaitingReply] = useState(false);

  const socketRef = useRef<WebSocket | null>(null);
  const stateRef = useRef<SessionState>(INITIAL_STATE);
  const reconnectAttemptRef = useRef(0);
  const heartbeatTimerRef = useRef<number | null>(null);
  const draftDebounceRef = useRef<number | null>(null);
  const pendingDraftBodyRef = useRef<string | null>(null);
  const closedByUserRef = useRef(false);
  const onTitleUpdatedRef = useRef(onTitleUpdated);
  const onUnknownEventRef = useRef(onUnknownEvent);
  // A ref, like the callbacks above: `connect` is a stable callback with empty
  // deps, so reading the prop directly would pin whatever it was on first
  // render — and a socket that reconnects would silently drop back to the other
  // vocabulary mid-session.
  const protocolRef = useRef(protocol);
  protocolRef.current = protocol;
  const warnedNativeRef = useRef(false);
  // Control frames that must not be lost across a reconnect (currently
  // only chat.stop). The WS-world analogue of an abortable chat transport.
  const pendingFramesRef = useRef<{ action: string; data: unknown }[]>([]);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    onTitleUpdatedRef.current = onTitleUpdated;
  }, [onTitleUpdated]);

  useEffect(() => {
    onUnknownEventRef.current = onUnknownEvent;
  }, [onUnknownEvent]);

  const send = useCallback((frame: { action: string; data: unknown }) => {
    const ws = socketRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(frame));
      return;
    }
    // Queue chat.stop so a stop clicked while the socket is reconnecting
    // is delivered on next OPEN instead of silently dropped. Draft updates
    // are intentionally NOT queued — they have a version guard and the
    // user's next keystroke will refresh the body anyway.
    if (frame.action === "chat.stop") {
      pendingFramesRef.current.push(frame);
    }
  }, []);

  const applyEvent = useCallback((frame: WsEvent) => {
    // Any of these means the queued window is over: the reply began, ended,
    // was cancelled, or the send failed outright.
    if (
      frame.event === "chat.stream_start" ||
      frame.event === "chat.stream_complete" ||
      frame.event === "chat.stream_error" ||
      frame.event === "chat.stream_cancelled" ||
      frame.event === "session.error"
    ) {
      setAwaitingReply(false);
    }
    // Side-effect events: handle BEFORE setState so React strict-mode's
    // double-invocation of the updater doesn't double-fire the effect.
    if (frame.event === "session.title_updated") {
      onTitleUpdatedRef.current?.();
      return;
    }
    if (frame.event === "session.error") {
      setLastError(frame.data.message);
      if (
        frame.data.code === "draft_version_mismatch" &&
        frame.data.detail &&
        typeof frame.data.detail === "object"
      ) {
        // Clear any pending optimistic body so the user's stale local
        // text doesn't auto-re-send with the new version.
        pendingDraftBodyRef.current = null;
        if (draftDebounceRef.current != null) {
          window.clearTimeout(draftDebounceRef.current);
          draftDebounceRef.current = null;
        }
      }
    }
    // The reducer ignores anything it does not know, which silently drops an
    // app-specific frame. Hand it over instead, so the container can act on
    // vocabulary the shared kit deliberately does not carry.
    if (!KNOWN_EVENTS.has(frame.event)) {
      onUnknownEventRef.current?.(frame);
      return;
    }
    setState((prev) => sessionReducer(prev, frame));
  }, []);

  const connect = useCallback(() => {
    if (closedByUserRef.current) return;
    // A half-read tool call from the previous connection must not be completed
    // by an ARGS event from this one — the ids are per-stream.
    resetAguiState();
    const path = `ws/canopy-sessions/${sessionId}/`;
    const built = wsUrl(path);
    const ws = new WebSocket(
      protocolRef.current === "ag-ui" ? withAguiProtocol(built) : built,
    );
    socketRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      reconnectAttemptRef.current = 0;
      // Flush any control frames that were queued while the socket was
      // closed. See `send` above.
      const queued = pendingFramesRef.current;
      pendingFramesRef.current = [];
      for (const frame of queued) {
        ws.send(JSON.stringify(frame));
      }
      if (heartbeatTimerRef.current != null) {
        window.clearInterval(heartbeatTimerRef.current);
      }
      heartbeatTimerRef.current = window.setInterval(() => {
        send({ action: "presence.heartbeat", data: {} });
      }, HEARTBEAT_INTERVAL_MS);
    };

    ws.onmessage = (e) => {
      try {
        const raw = JSON.parse(e.data);
        if (protocolRef.current === "ag-ui") {
          // We asked for AG-UI and the server answered in canopy's own frames:
          // a server that predates the negotiation, or a URL that lost the flag
          // on the way (see `withAguiProtocol`). The two are unambiguous — every
          // AG-UI event has a `type`, and no canopy frame does — so apply it as
          // what it is. Decoding it as AG-UI yields nothing, which is a blank
          // chat with no error: the worst possible way to find out.
          if (typeof raw?.type !== "string" && typeof raw?.event === "string") {
            if (!warnedNativeRef.current) {
              warnedNativeRef.current = true;
              console.warn(
                "canopy-ui: asked for protocol=ag-ui but the server sent canopy frames; handling them as canopy frames.",
              );
            }
            applyEvent(raw as WsEvent);
            return;
          }
          // One AG-UI event can be several canopy frames (a tool call is three
          // events) or none, so this is a fan-out rather than a rename.
          for (const frame of fromAgui(raw)) applyEvent(frame);
          return;
        }
        applyEvent(raw as WsEvent);
      } catch {
        // ignore malformed frames
      }
    };

    ws.onclose = () => {
      setConnected(false);
      if (heartbeatTimerRef.current != null) {
        window.clearInterval(heartbeatTimerRef.current);
        heartbeatTimerRef.current = null;
      }
      if (closedByUserRef.current) return;
      const attempt = reconnectAttemptRef.current;
      const delay =
        RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)];
      reconnectAttemptRef.current = attempt + 1;
      window.setTimeout(connect, delay);
    };

    ws.onerror = () => {
      // onclose will fire next; nothing to do here.
    };
  }, [applyEvent, send, sessionId, wsUrl]);

  useEffect(() => {
    closedByUserRef.current = false;
    reconnectAttemptRef.current = 0;
    connect();
    return () => {
      closedByUserRef.current = true;
      if (heartbeatTimerRef.current != null) {
        window.clearInterval(heartbeatTimerRef.current);
      }
      if (socketRef.current) {
        socketRef.current.close();
        socketRef.current = null;
      }
    };
  }, [connect]);

  const sendChat = useCallback(() => {
    // Flush the local body BEFORE committing. `chat.send` commits the SERVER's
    // draft, so this is the moment the body has to exist there — and when
    // live sync is off (single-player) it is the ONLY time it is sent.
    //
    // Unconditional on purpose: keying this off a pending debounce timer meant
    // nothing was flushed when there was no timer, which is now the normal case.
    if (draftDebounceRef.current != null) {
      window.clearTimeout(draftDebounceRef.current);
      draftDebounceRef.current = null;
    }
    if (pendingDraftBodyRef.current != null && stateRef.current.active_draft) {
      send({
        action: "draft.update",
        data: {
          version: stateRef.current.active_draft.version,
          body: pendingDraftBodyRef.current,
        },
      });
    }
    pendingDraftBodyRef.current = null;
    setAwaitingReply(true);
    send({ action: "chat.send", data: {} });
  }, [send]);

  const stopChat = useCallback(
    (messageId: string | null) => {
      // messageId is null when the turn is still queued. The server's
      // chat.stop cancels every non-terminal turn on the session and only
      // echoes the id back, so a null one cancels just as effectively.
      setAwaitingReply(false);
      send({ action: "chat.stop", data: { message_id: messageId } });
    },
    [send],
  );

  const updateDraft = useCallback(
    (body: string) => {
      // Optimistic local update so the textarea feels snappy.
      setState((prev) =>
        prev.active_draft
          ? { ...prev, active_draft: { ...prev.active_draft, body } }
          : prev,
      );
      pendingDraftBodyRef.current = body;
      // Alone in the session? Don't mirror keystrokes at all. The body is
      // flushed once by sendChat, which is the only moment the server actually
      // needs it. This is what makes single-player typing purely local — no
      // round trip, no echo, no version to disagree about.
      if (!shouldSyncDraftLive(stateRef.current.presence_user_ids)) {
        if (draftDebounceRef.current != null) {
          window.clearTimeout(draftDebounceRef.current);
          draftDebounceRef.current = null;
        }
        return;
      }
      if (draftDebounceRef.current != null) {
        window.clearTimeout(draftDebounceRef.current);
      }
      draftDebounceRef.current = window.setTimeout(() => {
        draftDebounceRef.current = null;
        const current = stateRef.current.active_draft;
        const pending = pendingDraftBodyRef.current;
        if (current != null && pending != null) {
          // Only consumed once it has actually gone out. Clearing it
          // unconditionally dropped anything typed before session.state
          // arrived (no draft yet ⇒ nothing sent, body forgotten).
          pendingDraftBodyRef.current = null;
          send({
            action: "draft.update",
            data: { version: current.version, body: pending },
          });
        }
      }, DRAFT_UPDATE_DEBOUNCE_MS);
    },
    [send],
  );

  const takeOverDraft = useCallback(() => {
    send({ action: "draft.take_over", data: {} });
  }, [send]);

  const discardDraft = useCallback(() => {
    send({ action: "draft.discard", data: {} });
  }, [send]);

  // Someone joining mid-compose must see what is ALREADY typed. Nothing was
  // mirrored while we were alone, so without this one catch-up flush their view
  // would sit empty until the next keystroke. Closes the only gap that skipping
  // live sync opens up.
  const liveSync = shouldSyncDraftLive(state.presence_user_ids);
  useEffect(() => {
    if (!liveSync) return;
    const pending = pendingDraftBodyRef.current;
    const current = stateRef.current.active_draft;
    if (pending == null || current == null) return;
    pendingDraftBodyRef.current = null;
    send({
      action: "draft.update",
      data: { version: current.version, body: pending },
    });
  }, [liveSync, send]);

  const prependMessages = useCallback((older: Message[]) => {
    // Apply a REST "Load earlier" page into the live socket state. A later
    // session.state snapshot (e.g. reconnect) resets to the tail — acceptable;
    // the user re-loads earlier if needed.
    setState((prev) => {
      const merged = prependHistory(prev.messages, older);
      return merged === prev.messages ? prev : { ...prev, messages: merged };
    });
  }, []);

  // `sendOverHttp`'s half of the hook: typing is local, sending is HTTP. Defined
  // here, after noteLocalSend, which it reuses for the pending row.
  const updateLocalDraft = useCallback((body: string) => {
    setLocalDraft({
      id: "local", slot: "next", status: "open", body, version: 0, last_editor: 0,
      last_edit_at: new Date().toISOString(),
    });
  }, []);

  const noteLocalSend = useCallback((text: string) => {
    setAwaitingReply(true);
    const body = text.trim();
    if (!body) return;
    // Routed through the ordinary `chat.user_message` case rather than a new
    // one, so the optimistic row goes in with the SAME dedupe the transcript
    // echo already relies on: when the agent reads the message and the runner
    // ships it back at its durable composite ordinal, the reducer matches on
    // recent identical text and merges instead of rendering it twice.
    setState((prev) =>
      sessionReducer(prev, {
        event: "chat.user_message",
        data: {
          // Local, and replaced by the server's id the moment the real row
          // arrives. Namespaced so it can never collide with one.
          message_id: `local:${Date.now()}`,
          turn_index: prev.messages.reduce((acc, m) => Math.max(acc, m.turn_index), 0) + 1,
          plaintext: body,
        },
      }),
    );
  }, []);

  const sendChatOverHttp = useCallback(() => {
    const body = (localDraft?.body ?? "").trim();
    if (!body || !sendOverHttp) return;
    noteLocalSend(body);
    setLocalDraft(null);
    sendOverHttp(body).catch((err: unknown) => {
      setAwaitingReply(false);
      setLastError(err instanceof Error ? err.message : "the message could not be sent");
      // Put the words back, so a failed send never costs what was typed.
      updateLocalDraft(body);
    });
  }, [localDraft, sendOverHttp, noteLocalSend, updateLocalDraft]);

  const exposedState = useMemo(
    () => (sendOverHttp ? { ...state, active_draft: localDraft } : state),
    [sendOverHttp, state, localDraft],
  );

  return {
    state: exposedState,
    connected,
    awaitingReply,
    sendChat: sendOverHttp ? sendChatOverHttp : sendChat,
    stopChat,
    updateDraft: sendOverHttp ? updateLocalDraft : updateDraft,
    takeOverDraft,
    discardDraft,
    prependMessages,
    noteLocalSend,
    lastError,
  };
}
