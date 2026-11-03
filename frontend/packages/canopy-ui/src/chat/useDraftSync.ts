import { useCallback, useEffect, useRef, useState, type Dispatch, type MutableRefObject, type SetStateAction } from "react";

import type { Draft, SessionState, TypingVisibility, WsEvent } from "./protocol";
import {
  TYPING_VISIBILITY_STORAGE_KEY,
  defaultDraftStorage,
  readStoredTypingVisibility,
  shouldSyncDraftLive,
  writeStoredTypingVisibility,
} from "./drafts";

const DRAFT_UPDATE_DEBOUNCE_MS = 150;

type Send = (frame: { action: string; data: unknown }) => boolean;

/**
 * The composer's draft, for `useSessionSocket`: what this person has typed,
 * whether and when it is mirrored to the server (`draft.update`), how others
 * see it while it is being typed (`typingVisibility`), and — for a principal
 * whose socket is read-only (`readOnly`) — a draft that stays in this browser.
 *
 * `handleFrame` takes every incoming frame before the reducer does; the hook
 * picks out the ones about its own draft.
 */
export function useDraftSync({
  send,
  setState,
  stateRef,
  socketRef,
  snapshotSeenRef,
  localDrafts,
  presenceUserIds,
}: {
  send: Send;
  setState: Dispatch<SetStateAction<SessionState>>;
  stateRef: MutableRefObject<SessionState>;
  socketRef: MutableRefObject<WebSocket | null>;
  /** The current connection's first snapshot has arrived. */
  snapshotSeenRef: MutableRefObject<boolean>;
  /** Never mirror keystrokes to the server (a private conversation). */
  localDrafts: boolean;
  presenceUserIds: number[];
}) {
  const localDraftsRef = useRef(localDrafts);
  localDraftsRef.current = localDrafts;
  // Per-browser, not per-session — a standing choice about how you type, read
  // once at mount from the same try/catch-safe storage `SendBox` uses.
  const [typingVisibility, setTypingVisibilityState] = useState<TypingVisibility>(
    () => readStoredTypingVisibility(defaultDraftStorage()) ?? "live",
  );
  const typingVisibilityRef = useRef(typingVisibility);
  typingVisibilityRef.current = typingVisibility;
  // A mode the person CHOSE while this socket could not carry it (closed, or
  // open but before the first snapshot told us what the server holds). Sent
  // once on the next snapshot, then cleared. Nothing else is ever pushed on
  // connect: a device with nothing stored has only a DEFAULT, and pushing a
  // default would expose words its author hid from another device.
  const pendingVisibilityRef = useRef<TypingVisibility | null>(null);
  const draftDebounceRef = useRef<number | null>(null);
  // The latest body typed and not yet sent to the server.
  const pendingDraftBodyRef = useRef<string | null>(null);
  // The local draft a read-only principal types into.
  const [localDraft, setLocalDraft] = useState<Draft | null>(null);

  const adoptVisibility = useCallback((visibility: TypingVisibility) => {
    if (visibility === typingVisibilityRef.current) return;
    typingVisibilityRef.current = visibility;
    setTypingVisibilityState(visibility);
    writeStoredTypingVisibility(defaultDraftStorage(), visibility);
  }, []);

  // A native `storage` event fires in every OTHER tab the instant one tab
  // writes the key, so it carries a mode change to a tab with no live socket.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onStorage = (e: StorageEvent) => {
      if (e.key !== TYPING_VISIBILITY_STORAGE_KEY) return;
      const next = readStoredTypingVisibility(defaultDraftStorage());
      if (next != null && next !== typingVisibilityRef.current) {
        typingVisibilityRef.current = next;
        setTypingVisibilityState(next);
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const clearDebounce = useCallback(() => {
    if (draftDebounceRef.current != null) {
      window.clearTimeout(draftDebounceRef.current);
      draftDebounceRef.current = null;
    }
  }, []);

  const handleFrame = useCallback((frame: WsEvent) => {
    // `draft.updated` reaches only the AUTHOR's own sockets, so this is always
    // MY draft echoed — including a mode chosen in another tab or on another
    // device. The mode changes server-side only on an explicit choice
    // (`draft.set_visibility`), so the last choice wins.
    if (frame.event === "draft.updated" && frame.data.visibility) {
      adoptVisibility(frame.data.visibility);
    }
    // On a snapshot the SERVER's stored mode is the truth — the last explicit
    // choice from any tab or device — except a choice made HERE while the
    // socket could not carry it, which is newer and is sent now. No own draft
    // (a viewer or a contact) means there is nothing of ours to set.
    if (frame.event === "session.state") {
      const ad = frame.data.active_draft;
      const pending = pendingVisibilityRef.current;
      pendingVisibilityRef.current = null;
      if (ad != null && pending != null) {
        send({ action: "draft.set_visibility", data: { visibility: pending } });
      } else if (ad?.visibility) {
        adoptVisibility(ad.visibility);
      }
    }
    // A body conflict: drop the stale optimistic body so it does not re-send
    // under the new version. The mode never rides `draft.update`, so it is
    // unaffected.
    if (
      frame.event === "session.error" &&
      frame.data.code === "draft_version_mismatch" &&
      frame.data.detail &&
      typeof frame.data.detail === "object"
    ) {
      pendingDraftBodyRef.current = null;
      clearDebounce();
    }
  }, [send, adoptVisibility, clearDebounce]);

  const updateDraft = useCallback(
    (body: string) => {
      // Optimistic local update so the textarea feels snappy.
      setState((prev) =>
        prev.active_draft ? { ...prev, active_draft: { ...prev.active_draft, body } } : prev,
      );
      pendingDraftBodyRef.current = body;
      // Alone in the session, keystrokes are not mirrored at all: the body is
      // flushed once by the send, the only moment the server needs it.
      if (localDraftsRef.current || !shouldSyncDraftLive(stateRef.current.presence_user_ids)) {
        clearDebounce();
        return;
      }
      clearDebounce();
      draftDebounceRef.current = window.setTimeout(() => {
        draftDebounceRef.current = null;
        const current = stateRef.current.active_draft;
        const pending = pendingDraftBodyRef.current;
        // Consumed only once it has actually gone out: before the snapshot
        // there is no draft to version it against, and the body must wait.
        if (current != null && pending != null) {
          pendingDraftBodyRef.current = null;
          send({ action: "draft.update", data: { version: current.version, body: pending } });
        }
      }, DRAFT_UPDATE_DEBOUNCE_MS);
    },
    [send, setState, stateRef, clearDebounce],
  );

  /** The body a send carries: the composer's own text when given, else the
   *  last one typed. Stops any mirroring still waiting — the send carries it. */
  const takeBody = useCallback((text?: string): string => {
    clearDebounce();
    const body = (text ?? pendingDraftBodyRef.current ?? stateRef.current.active_draft?.body ?? "").trim();
    pendingDraftBodyRef.current = null;
    return body;
  }, [stateRef, clearDebounce]);

  const setTypingVisibility = useCallback(
    (visibility: TypingVisibility) => {
      setTypingVisibilityState(visibility);
      typingVisibilityRef.current = visibility;
      writeStoredTypingVisibility(defaultDraftStorage(), visibility);
      // Its own idempotent frame, sent even while alone: a peer who joins later
      // reads the mode straight off their connect snapshot. Until the first
      // snapshot (or while the socket is closed) the choice waits for it.
      const ws = socketRef.current;
      const open = ws != null && ws.readyState === WebSocket.OPEN;
      if (!open || !snapshotSeenRef.current) {
        pendingVisibilityRef.current = visibility;
        return;
      }
      if (stateRef.current.active_draft == null) return;
      send({ action: "draft.set_visibility", data: { visibility } });
    },
    [send, socketRef, snapshotSeenRef, stateRef],
  );

  const discardDraft = useCallback(() => {
    send({ action: "draft.discard", data: {} });
  }, [send]);

  // Someone joining mid-compose sees what is ALREADY typed: nothing was
  // mirrored while alone, so flush it once when live sync turns on.
  const liveSync = !localDrafts && shouldSyncDraftLive(presenceUserIds);
  useEffect(() => {
    if (!liveSync) return;
    const pending = pendingDraftBodyRef.current;
    const current = stateRef.current.active_draft;
    if (pending == null || current == null) return;
    pendingDraftBodyRef.current = null;
    send({ action: "draft.update", data: { version: current.version, body: pending } });
  }, [liveSync, send, stateRef]);

  // The read-only principal's draft: typing is local, sending is HTTP.
  const updateLocalDraft = useCallback((body: string) => {
    setLocalDraft({
      id: "local", slot: "next", status: "open", body, version: 0, last_editor: 0,
      last_edit_at: new Date().toISOString(),
    });
  }, []);

  /** Put a failed send's words back — only if nothing newer has been typed
   *  since. The draft was cleared when the send went out, so an empty one
   *  means the person has not started anything else; if they have, that stays
   *  exactly as they left it. */
  const restoreFailedLocalDraft = useCallback((body: string) => {
    setLocalDraft((prev) =>
      prev == null || prev.body === ""
        ? {
            id: "local", slot: "next", status: "open", body, version: 0,
            last_editor: 0, last_edit_at: new Date().toISOString(),
          }
        : prev,
    );
  }, []);

  const clearLocalDraft = useCallback(() => setLocalDraft(null), []);

  return {
    typingVisibility,
    setTypingVisibility,
    handleFrame,
    updateDraft,
    takeBody,
    discardDraft,
    localDraft,
    updateLocalDraft,
    restoreFailedLocalDraft,
    clearLocalDraft,
  };
}

export type DraftSync = ReturnType<typeof useDraftSync>;
