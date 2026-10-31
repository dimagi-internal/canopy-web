import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { createAguiDecoder } from "./agui";
import type { Message, SessionState, TypingVisibility, WsEvent } from "./protocol";
import { prependHistory } from "./history";
import { REDUCER_EVENTS, sessionReducer } from "./sessionReducer";
import { useDraftSync } from "./useDraftSync";
import { newClientId, useSendReceipts } from "./useSendReceipts";

export { newClientId };

const HEARTBEAT_INTERVAL_MS = 20_000;
const RECONNECT_DELAYS_MS = [1_000, 2_000, 5_000, 10_000];
/** Close codes that are canopy's ANSWER, not a dropped line: 4003 (you may not
 *  open this conversation — its ACL, the site's agents, or a private one you are
 *  not in or hold no key for) and 4004 (there is no such conversation). Asking
 *  again gets the same answer, so the hook stops and says so (`refused`). */
export const REFUSAL_CLOSE_CODES: readonly number[] = [4003, 4004];
const INITIAL_STATE: SessionState = {
  messages: [],
  active_draft: null,
  participants: [],
  presence_user_ids: [],
  current_user_id: null,
};

export interface UseSessionSocketOptions {
  /** The chat session id (UUID string). */
  sessionId: string;
  /**
   * App-injected WebSocket URL builder. The kit never imports app routing/base
   * helpers; the container passes one (e.g. canopy's `wsUrl`). Called with the
   * relative path `ws/canopy-sessions/${sessionId}/`.
   *
   * May return a Promise: it is called before EVERY connection, reconnects
   * included, so a container can fetch a one-time ticket each time and keep
   * its token off the URL (canopy-client's `sessionSocketTicketUrl`). A
   * rejected promise is treated like a dropped connection and retried.
   */
  wsUrl: (path: string) => string | Promise<string>;
  /**
   * Optional side-effect callback fired when the server broadcasts a
   * `session.title_updated`, with the new title. The kit has no opinion on
   * what to do with it.
   */
  onTitleUpdated?: (title?: string) => void;
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
   *
   * Must pass `clientId` through as the send's `client_id` (both `/api/canopy-
   * sessions/{id}/send` and `/api/contact/sessions/{id}/send` accept it) — it
   * is the SAME id `sendChatOverHttp` stamps on the optimistic row, and without
   * it the server's queued-turn projection has no way to recognise that row as
   * the one it already showed, so the sender's own send rendered a second time
   * as an unowned "queued" placeholder.
   */
  sendOverHttp?: (text: string, clientId: string) => Promise<unknown>;
  /**
   * The HTTP route for a USER's send, used only when a socket send gets no
   * receipt (see `SEND_RECEIPT_TIMEOUT_MS`). Must pass `clientId` through as the
   * send's `client_id`: the server derives the turn's idempotency key from it,
   * so a socket frame that did arrive late and this resend make ONE turn.
   * Without it a send on a dead socket is marked "not sent" instead.
   */
  resendOverHttp?: (text: string, clientId: string) => Promise<unknown>;
  /**
   * Keep the composer's draft in this browser: never mirror keystrokes to the server
   * (`draft.update`), even with others present. For a conversation whose words the
   * server must not hold — canopy's private (sensitive) conversations — where the text
   * reaches the server only inside the send. Peers then see no "typing" preview.
   */
  localDrafts?: boolean;
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
  /** This connection's snapshot (`session.state`) has arrived. Until it does an
   *  empty transcript is unknown rather than empty — show "loading", not
   *  "start the conversation". */
  synced: boolean;
  /** A send is outstanding with no reply yet — the turn is QUEUED, waiting for
   *  a runner. Nothing in `state` can express this (there is no assistant
   *  message until the first token), and it is what keeps Stop reachable while
   *  a turn is stuck. */
  awaitingReply: boolean;
  /** `text` is what the composer shows; without it, the last body `updateDraft`
   *  was given. Pass it — a body restored from storage never reached `updateDraft`. */
  sendChat: (text?: string) => void;
  stopChat: (messageId: string | null) => void;
  /** Withdraw one of your own queued sends (a `QueuedMessage.turn_id`). Its row
   *  leaves the screen at once; the server's `session.queued` confirms it. */
  unqueueChat: (turnId: string) => void;
  updateDraft: (body: string) => void;
  /** @deprecated ignored since 0.13 — everyone has their own draft now, so
   *  there is no lock left to take over. Kept as a no-op so a host still
   *  passing it through (e.g. ace-web's `onTakeOver={socket.takeOverDraft}`)
   *  keeps compiling across the upgrade. */
  takeOverDraft: () => void;
  discardDraft: () => void;
  /** Merge an older REST page into the transcript. `hasMoreBefore` says
   *  whether the server holds anything older still (`false` after loading
   *  everything); it is kept across reconnects with the rows. */
  prependMessages: (older: Message[], opts?: { hasMoreBefore?: boolean }) => void;
  /** The HOST sent a message for this session over HTTP rather than through
   *  this socket (the embedded widget's first message, before a socket exists;
   *  `ChatPage`'s `/share` command): show it, as delivered, and wait for a
   *  reply. The line is not a server row until the agent's transcript ships it
   *  back, so without this the send changes nothing on screen.
   *
   *  Pass the SAME `clientId` the matching REST body sent (`sendChatOverHttp`
   *  does this internally; a host driving its own HTTP send — the widget's
   *  first message, `ChatPage`'s `/share` command — must generate one and pass
   *  it to both), so the optimistic row's `content.client_id` matches the
   *  server's queued-turn projection and `QueuedRows.hideClientIds` can tell
   *  they're the same send rather than rendering it twice. */
  noteLocalSend: (text: string, clientId?: string) => void;
  lastError: string | null;
  /** canopy turned this connection away for good (`REFUSAL_CLOSE_CODES`): the
   *  hook has stopped reconnecting, and the host should say the conversation is
   *  not available rather than show a chat that is "Reconnecting…" forever.
   *  `reason` is the server's own words when it sent any (a `session.error` just
   *  before closing, e.g. `needs_key`); "" otherwise. Null while connecting, open,
   *  or merely dropped. */
  refused: { code: number; reason: string } | null;
  /** This person's chosen mode for how their OWN in-progress message appears
   *  to others — persisted per browser (`canopy.chat.typingVisibility`).
   *  Defaults to `"live"`, today's behaviour. */
  typingVisibility: TypingVisibility;
  /** Change the mode. When there is a non-empty draft body and live sync is
   *  on (somebody else is present), sends one `draft.update` immediately so
   *  peers switch right away rather than waiting for the next keystroke. */
  setTypingVisibility: (visibility: TypingVisibility) => void;
}

// Frames the reducer handles: its own list, so the two cannot drift (see
// `REDUCER_EVENTS`). Anything else goes to `onUnknownEvent` rather than being
// dropped.
const KNOWN_EVENTS = REDUCER_EVENTS;

/**
 * The live session over one WebSocket: the transport (connect, reconnect,
 * heartbeat, the refusal close codes, AG-UI decoding) and the reducer's state.
 * The send path is `useSendReceipts` and the composer's draft `useDraftSync`;
 * each takes every frame before the reducer does (`handleFrame`).
 */
export function useSessionSocket({
  sessionId,
  wsUrl,
  onTitleUpdated,
  onUnknownEvent,
  protocol = "canopy",
  sendOverHttp,
  resendOverHttp,
  localDrafts = false,
}: UseSessionSocketOptions): UseSessionSocketResult {
  const [state, setState] = useState<SessionState>(INITIAL_STATE);
  const [connected, setConnected] = useState(false);
  // The current connection's snapshot has arrived: until then an empty
  // transcript is unknown, not empty.
  const [synced, setSynced] = useState(false);
  const [lastError, setLastError] = useState<string | null>(null);
  const [refused, setRefused] = useState<{ code: number; reason: string } | null>(null);
  // The server's `session.error` on the CURRENT connection, so a refusal that
  // follows it can say why.
  const connectionErrorRef = useRef<string>("");
  // A send has gone out but no reply has begun — the turn is QUEUED. There is
  // no assistant row in that window, so nothing in `state` can say it, and
  // without it Stop is unreachable exactly when the turn is stuck.
  const [awaitingReply, setAwaitingReply] = useState(false);

  const socketRef = useRef<WebSocket | null>(null);
  const stateRef = useRef<SessionState>(INITIAL_STATE);
  const reconnectAttemptRef = useRef(0);
  const heartbeatTimerRef = useRef<number | null>(null);
  const closedByUserRef = useRef(false);
  const generationRef = useRef(0);
  const onTitleUpdatedRef = useRef(onTitleUpdated);
  const onUnknownEventRef = useRef(onUnknownEvent);
  // A ref, like the callbacks above: `connect` is a stable callback with empty
  // deps, so reading the prop directly would pin whatever it was on first
  // render — and a socket that reconnects would silently drop back to the other
  // vocabulary mid-session.
  const protocolRef = useRef(protocol);
  protocolRef.current = protocol;
  const warnedNativeRef = useRef(false);
  // Control frames that must not be lost across a reconnect (currently only
  // chat.stop), flushed on the next open.
  const pendingFramesRef = useRef<{ action: string; data: unknown }[]>([]);
  const snapshotSeenRef = useRef(false);
  const resendRef = useRef(resendOverHttp);
  resendRef.current = resendOverHttp;

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
      return true;
    }
    // A stop clicked while the socket is reconnecting is delivered on the next
    // open instead of dropped. Draft updates are not queued: they have a
    // version guard and the next keystroke refreshes the body anyway.
    if (frame.action === "chat.stop") {
      pendingFramesRef.current.push(frame);
    }
    return false;
  }, []);

  const drafts = useDraftSync({
    send,
    setState,
    stateRef,
    socketRef,
    snapshotSeenRef,
    localDrafts,
    presenceUserIds: state.presence_user_ids,
  });
  const receipts = useSendReceipts({
    send,
    setState,
    setAwaitingReply,
    setLastError,
    resendOverHttp: resendRef,
    sendOverHttp,
    drafts,
  });
  const { handleFrame: draftFrame } = drafts;
  const { handleFrame: receiptFrame, rescueAll } = receipts;

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
    receiptFrame(frame);
    if (frame.event === "session.state") {
      snapshotSeenRef.current = true;
      setSynced(true);
    }
    draftFrame(frame);
    // Side-effect events: handled BEFORE setState so React strict-mode's
    // double-invocation of the updater doesn't double-fire the effect.
    if (frame.event === "session.title_updated") {
      onTitleUpdatedRef.current?.(frame.data?.title);
      return;
    }
    if (frame.event === "session.error") {
      setLastError(frame.data.message);
      connectionErrorRef.current = frame.data.message ?? "";
    }
    // The reducer ignores anything it does not know, which would silently drop
    // an app-specific frame. Hand it over instead.
    if (!KNOWN_EVENTS.has(frame.event)) {
      onUnknownEventRef.current?.(frame);
      return;
    }
    setState((prev) => sessionReducer(prev, frame));
  }, [draftFrame, receiptFrame]);

  const connect = useCallback(() => {
    if (closedByUserRef.current) return;
    // Which mount this attempt belongs to. An async URL may resolve after the
    // hook re-mounted for another session; it must not open a socket then.
    const generation = generationRef.current;
    const path = `ws/canopy-sessions/${sessionId}/`;
    const built = wsUrl(path);
    if (typeof built !== "string") {
      // An async builder (a fresh one-time ticket per connection). If it fails,
      // retry on the same backoff a dropped socket uses; if the hook was torn
      // down meanwhile, open nothing.
      built.then(
        (url) => {
          if (!closedByUserRef.current && generation === generationRef.current) openSocket(url);
        },
        () => {
          if (closedByUserRef.current || generation !== generationRef.current) return;
          const attempt = reconnectAttemptRef.current;
          reconnectAttemptRef.current = attempt + 1;
          window.setTimeout(connect,
            RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)]);
        },
      );
      return;
    }
    openSocket(built);

    // Declared inside `connect` so it shares exactly this render's handlers.
    function openSocket(built: string) {
    const ws = new WebSocket(
      protocolRef.current === "ag-ui" ? withAguiProtocol(built) : built,
    );
    socketRef.current = ws;
    // This connection's AG-UI decoder: a tool call's head waits for its
    // arguments, and that pairing belongs to the stream it arrived on.
    const decoder = createAguiDecoder();

    connectionErrorRef.current = "";
    ws.onopen = () => {
      snapshotSeenRef.current = false;
      setSynced(false);
      setConnected(true);
      reconnectAttemptRef.current = 0;
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
          // what it is rather than decoding it to nothing.
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
          for (const frame of decoder.decode(raw)) applyEvent(frame);
          return;
        }
        applyEvent(raw as WsEvent);
      } catch {
        // ignore malformed frames
      }
    };

    ws.onclose = (event?: { code?: number }) => {
      snapshotSeenRef.current = false;
      setConnected(false);
      // A send still waiting for its receipt will not get one on this socket.
      rescueAll();
      if (heartbeatTimerRef.current != null) {
        window.clearInterval(heartbeatTimerRef.current);
        heartbeatTimerRef.current = null;
      }
      if (closedByUserRef.current) return;
      // canopy said no. Retrying would only hear no again, behind a
      // "Reconnecting…" chip over an empty transcript.
      const code = event?.code;
      if (code != null && REFUSAL_CLOSE_CODES.includes(code)) {
        setAwaitingReply(false);
        setRefused({ code, reason: connectionErrorRef.current });
        return;
      }
      const attempt = reconnectAttemptRef.current;
      const delay =
        RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)];
      reconnectAttemptRef.current = attempt + 1;
      window.setTimeout(connect, delay);
    };

    ws.onerror = () => {
      // onclose will fire next; nothing to do here.
    };
    }
  }, [applyEvent, rescueAll, send, sessionId, wsUrl]);

  useEffect(() => {
    closedByUserRef.current = false;
    reconnectAttemptRef.current = 0;
    setRefused(null);
    connect();
    return () => {
      generationRef.current += 1;
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

  const unqueueChat = useCallback(
    (turnId: string) => {
      setState((prev) => {
        const entry = (prev.queued ?? []).find((q) => q.turn_id === turnId);
        const clientId = entry?.client_id || null;
        const queued = (prev.queued ?? []).filter((q) => q.turn_id !== turnId);
        // Your own line is drawn by its optimistic row (keyed by client_id),
        // not by the queued list — take it down too, or it reads as still sent.
        const messages = clientId
          ? prev.messages.filter((m) => !(m.role === "user" && m.content?.client_id === clientId))
          : prev.messages;
        // Keep its words, in place and marked, until the server's list says so.
        const cancelled = entry && !(prev.cancelled ?? []).some((c) => c.turn_id === turnId)
          ? [...(prev.cancelled ?? []),
             { turn_id: entry.turn_id, client_id: entry.client_id, author: entry.author, text: entry.text,
               sent_at: entry.sent_at, cancelled_by: "" }]
          : prev.cancelled;
        return { ...prev, queued, messages, cancelled };
      });
      send({ action: "chat.unqueue", data: { turn_id: turnId } });
    },
    [send],
  );

  // @deprecated no-op: everyone has their own draft, so there is no lock to
  // take over. The server accepts and ignores `draft.take_over` too.
  const takeOverDraft = useCallback(() => undefined, []);

  const prependMessages = useCallback((older: Message[], opts?: { hasMoreBefore?: boolean }) => {
    // A REST scroll-back page into the live state. The reducer keeps it across
    // a reconnect's snapshot (`keepLoadedHistory`), and with it whether the
    // server holds anything older still.
    setState((prev) => {
      const merged = prependHistory(prev.messages, older);
      const more = opts?.hasMoreBefore;
      if (merged === prev.messages && (more === undefined || more === prev.has_more_before)) return prev;
      return { ...prev, messages: merged, ...(more !== undefined ? { has_more_before: more } : {}) };
    });
  }, []);

  const exposedState = useMemo(
    () => (sendOverHttp ? { ...state, active_draft: drafts.localDraft } : state),
    [sendOverHttp, state, drafts.localDraft],
  );

  return {
    state: exposedState,
    connected,
    synced,
    awaitingReply,
    sendChat: sendOverHttp ? receipts.sendChatOverHttp : receipts.sendChat,
    stopChat,
    unqueueChat,
    updateDraft: sendOverHttp ? drafts.updateLocalDraft : drafts.updateDraft,
    takeOverDraft,
    discardDraft: drafts.discardDraft,
    prependMessages,
    noteLocalSend: receipts.noteLocalSend,
    lastError,
    refused,
    typingVisibility: drafts.typingVisibility,
    setTypingVisibility: drafts.setTypingVisibility,
  };
}
