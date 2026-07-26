import { useCallback, useEffect, useRef, useState } from "react";

import type { Message, SessionState, WsEvent } from "./protocol";
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
  lastError: string | null;
}

export function useSessionSocket({
  sessionId,
  wsUrl,
  onTitleUpdated,
}: UseSessionSocketOptions): UseSessionSocketResult {
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
  // Control frames that must not be lost across a reconnect (currently
  // only chat.stop). The WS-world analogue of an abortable chat transport.
  const pendingFramesRef = useRef<{ action: string; data: unknown }[]>([]);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    onTitleUpdatedRef.current = onTitleUpdated;
  }, [onTitleUpdated]);

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
    setState((prev) => sessionReducer(prev, frame));
  }, []);

  const connect = useCallback(() => {
    if (closedByUserRef.current) return;
    const ws = new WebSocket(wsUrl(`ws/canopy-sessions/${sessionId}/`));
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
        const frame = JSON.parse(e.data) as WsEvent;
        applyEvent(frame);
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

  return {
    state,
    connected,
    awaitingReply,
    sendChat,
    stopChat,
    updateDraft,
    takeOverDraft,
    discardDraft,
    prependMessages,
    lastError,
  };
}
