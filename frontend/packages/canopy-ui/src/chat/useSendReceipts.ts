import { useCallback, useRef, type Dispatch, type MutableRefObject, type SetStateAction } from "react";

import type { SessionState, WsEvent } from "./protocol";
import { markSendDelivered, markSendFailed, withPendingSend } from "./sessionReducer";
import type { DraftSync } from "./useDraftSync";

/** How long a socket send may go without the server's receipt before the page
 *  resends it over HTTP. Short on purpose: the receipt normally arrives within
 *  a round trip, and a missing one means the socket is dead (a phone resuming
 *  from the background can hold one that still reads OPEN). */
const SEND_RECEIPT_TIMEOUT_MS = 4_000;
const NOT_SENT =
  "Not sent: the connection dropped before canopy got it. Your words are kept here, so copy them and send again.";

type Send = (frame: { action: string; data: unknown }) => boolean;

/**
 * A client-generated nonce for a send's `client_id` — exported so a host
 * driving its own HTTP send (the widget's opening message, before a socket
 * exists to send over; `ChatPage`'s `/share` command) can mint the SAME id
 * for both the REST body and the matching `noteLocalSend` call, which is what
 * lets `QueuedRows.hideClientIds` recognise the two as one send rather than
 * rendering it twice.
 */
export function newClientId(): string {
  // An idempotency nonce, not a secret — but it rides a request, so it comes
  // from the platform CSPRNG, never Math.random (CodeQL js/insecure-randomness).
  // randomUUID needs a secure context; getRandomValues does not.
  const c = (globalThis as {
    crypto?: { randomUUID?: () => string; getRandomValues?: (a: Uint8Array) => Uint8Array };
  }).crypto;
  if (c?.randomUUID) return c.randomUUID();
  if (c?.getRandomValues) {
    const bytes = c.getRandomValues(new Uint8Array(16));
    return `c${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
  }
  return `c${Date.now().toString(36)}${(clientIdCounter++).toString(36)}`;
}

let clientIdCounter = 0;

/**
 * Every send, for `useSessionSocket`, and what becomes of it. Each one goes
 * on screen at once as a pending row (`withPendingSend`) and ends either
 * delivered (`markSendDelivered`: the server's receipt, or the HTTP send
 * succeeding) or failed (`markSendFailed`, words kept) — never silently.
 *
 * Socket sends carry their text and a client id; without the server's receipt
 * in `SEND_RECEIPT_TIMEOUT_MS` (or when the socket closes) they are resent over
 * HTTP under the same id, so a late socket frame and the resend make one turn.
 */
export function useSendReceipts({
  send,
  setState,
  setAwaitingReply,
  setLastError,
  resendOverHttp,
  sendOverHttp,
  drafts,
}: {
  send: Send;
  setState: Dispatch<SetStateAction<SessionState>>;
  setAwaitingReply: (v: boolean) => void;
  setLastError: (v: string | null) => void;
  resendOverHttp: MutableRefObject<((text: string, clientId: string) => Promise<unknown>) | undefined>;
  sendOverHttp?: (text: string, clientId: string) => Promise<unknown>;
  drafts: DraftSync;
}) {
  // Sends awaiting the server's receipt: client_id -> {text, timer}.
  const unconfirmedRef = useRef(new Map<string, { text: string; timer: number | null }>());

  // A send that got no receipt: resend it over HTTP under the same client_id,
  // or, with no HTTP route, say plainly that it did not go.
  const rescueSend = useCallback((clientId: string) => {
    const entry = unconfirmedRef.current.get(clientId);
    if (!entry) return; // the receipt got here first
    if (entry.timer != null) window.clearTimeout(entry.timer);
    unconfirmedRef.current.delete(clientId);
    const resend = resendOverHttp.current;
    if (!resend) {
      setAwaitingReply(false);
      setState((prev) => markSendFailed(prev, clientId, NOT_SENT));
      return;
    }
    resend(entry.text, clientId).then(
      () => setState((prev) => markSendDelivered(prev, clientId)),
      () => {
        setAwaitingReply(false);
        setState((prev) => markSendFailed(prev, clientId, NOT_SENT));
      },
    );
  }, [resendOverHttp, setAwaitingReply, setState]);

  /** The server's receipt for one of OUR sends: stop waiting on it. */
  const handleFrame = useCallback((frame: WsEvent) => {
    if (frame.event === "draft.committed" && frame.data.client_id) {
      const entry = unconfirmedRef.current.get(frame.data.client_id);
      if (entry?.timer != null) window.clearTimeout(entry.timer);
      unconfirmedRef.current.delete(frame.data.client_id);
    }
  }, []);

  /** The socket closed: a send still waiting for its receipt will not get one on it. */
  const rescueAll = useCallback(() => {
    for (const clientId of [...unconfirmedRef.current.keys()]) rescueSend(clientId);
  }, [rescueSend]);

  const { takeBody } = drafts;
  const sendChat = useCallback((text?: string) => {
    const body = takeBody(text);
    if (!body) return;
    const clientId = newClientId();
    setAwaitingReply(true);
    setState((prev) => withPendingSend(prev, { clientId, text: body }));
    unconfirmedRef.current.set(clientId, { text: body, timer: null });
    const sent = send({ action: "chat.send", data: { client_id: clientId, text: body } });
    if (!sent) {
      rescueSend(clientId);
      return;
    }
    const entry = unconfirmedRef.current.get(clientId);
    if (entry) entry.timer = window.setTimeout(() => rescueSend(clientId), SEND_RECEIPT_TIMEOUT_MS);
  }, [takeBody, send, rescueSend, setAwaitingReply, setState]);

  /** A send the HOST already made over HTTP: shown, as delivered, and a reply
   *  awaited. The transcript's echo later adopts the row (by client id, else by
   *  its words), so it never shows twice. */
  const noteLocalSend = useCallback((text: string, clientId?: string) => {
    setAwaitingReply(true);
    const body = text.trim();
    if (!body) return;
    const cid = clientId ?? newClientId();
    setState((prev) => markSendDelivered(withPendingSend(prev, { clientId: cid, text: body }), cid));
  }, [setAwaitingReply, setState]);

  // A read-only principal's send: over HTTP, pending until it answers.
  const { localDraft, clearLocalDraft, restoreFailedLocalDraft } = drafts;
  const sendChatOverHttp = useCallback((text?: string) => {
    const body = (text ?? localDraft?.body ?? "").trim();
    if (!body || !sendOverHttp) return;
    // ONE id for both halves of this send — the pending row and the REST body —
    // so the server's queued-turn projection and this row compare equal.
    const clientId = newClientId();
    setAwaitingReply(true);
    setState((prev) => withPendingSend(prev, { clientId, text: body }));
    clearLocalDraft();
    sendOverHttp(body, clientId).then(
      () => setState((prev) => markSendDelivered(prev, clientId)),
      (err: unknown) => {
        const detail = err instanceof Error && err.message ? err.message : "the message could not be sent";
        setAwaitingReply(false);
        setLastError(detail);
        setState((prev) => markSendFailed(prev, clientId, `Not sent: ${detail}`));
        // Put the words back in the box too, unless something newer is there.
        restoreFailedLocalDraft(body);
      },
    );
  }, [localDraft, sendOverHttp, clearLocalDraft, restoreFailedLocalDraft, setAwaitingReply, setLastError, setState]);

  return { sendChat, sendChatOverHttp, noteLocalSend, handleFrame, rescueAll };
}
