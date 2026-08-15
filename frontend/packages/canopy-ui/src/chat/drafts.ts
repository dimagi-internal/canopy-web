import type { Draft } from "./protocol";

export const IDLE_THRESHOLD_MS = 2_000;

export function isDraftIdle(draft: Draft | null | undefined): boolean {
  if (!draft?.last_edit_at) return true;
  return Date.now() - new Date(draft.last_edit_at).getTime() > IDLE_THRESHOLD_MS;
}

/**
 * Whether keystrokes need to be mirrored to the server AS YOU TYPE.
 *
 * The co-edited draft only earns its cost when somebody else is looking at it.
 * Alone in a session — the overwhelmingly common case — a per-keystroke
 * `draft.update` buys nothing and costs a round trip, a re-render on the echo,
 * and a version to disagree about. The body still reaches the server once,
 * right before `chat.send` (which commits the SERVER's copy), so sending is
 * unaffected; see useSessionSocket.sendChat.
 *
 * Presence includes yourself, so "alone" is a set of 0 or 1. An empty set means
 * presence has not arrived yet — treated as alone, since the pre-connect window
 * is exactly when there is no one to sync with.
 */
export function shouldSyncDraftLive(presenceUserIds: readonly number[]): boolean {
  return presenceUserIds.length > 1;
}

/**
 * Milliseconds until the draft lock becomes idle. Returns 0 if already
 * idle. Use this to schedule a timer that forces a re-render at the
 * idle transition point.
 */
export function msUntilDraftIdle(draft: Draft | null | undefined): number {
  if (!draft?.last_edit_at) return 0;
  const elapsed = Date.now() - new Date(draft.last_edit_at).getTime();
  return Math.max(0, IDLE_THRESHOLD_MS - elapsed);
}

// ---------------------------------------------------------------------------
// Composer persistence — surviving a navigation the server never hears about.
//
// The composer is local-first (see SendBox) and, alone in a session,
// `shouldSyncDraftLive` keeps it that way: nothing is mirrored to the server
// until the moment before `chat.send`. Those two facts are right on their own
// and, together, mean a half-typed message dies the instant SendBox unmounts —
// route away from /chat/:id and back and the box is empty, with no copy of the
// text anywhere. (Adopting your OWN server draft on mount wouldn't fix it:
// single-player never put one there.)
//
// So the copy has to be local too. localStorage rather than sessionStorage
// because "come back to it" includes closing the tab; per-browser, deliberately
// not synced across devices.
// ---------------------------------------------------------------------------

/** Just the slice of `Storage` this needs — so tests can pass a plain fake and
 *  a host can pass sessionStorage if it prefers per-tab drafts. */
export interface DraftStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

const DRAFT_STORAGE_PREFIX = "canopy.chat.draft.";

/** Drafts older than this are treated as gone. A month-old half-sentence is
 *  not something you meant to come back to, and restoring it into a live
 *  session is worse than losing it. */
export const DRAFT_STORAGE_TTL_MS = 7 * 24 * 60 * 60 * 1000;

export function draftStorageKey(persistKey: string): string {
  return `${DRAFT_STORAGE_PREFIX}${persistKey}`;
}

/**
 * `window.localStorage` when it is usable, else null.
 *
 * Merely TOUCHING the property throws in a sandboxed iframe or with
 * third-party storage blocked, so this is a try/catch and not a truthiness
 * check. Null disables persistence and changes nothing else — typing must
 * never depend on storage being available.
 */
export function defaultDraftStorage(): DraftStorage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

/**
 * The stored body for a session, or null when there is nothing worth
 * restoring. Anything unreadable — absent, malformed, wrong shape, expired —
 * is dropped and reported as null, and an expired or corrupt entry is pruned on
 * the way past so it cannot accumulate.
 */
export function readStoredDraft(
  storage: DraftStorage | null,
  persistKey: string,
  now: number = Date.now(),
): string | null {
  if (!storage || !persistKey) return null;
  const key = draftStorageKey(persistKey);
  let raw: string | null = null;
  try {
    raw = storage.getItem(key);
  } catch {
    return null;
  }
  if (raw == null) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    remove(storage, key);
    return null;
  }

  const record = parsed as { body?: unknown; at?: unknown } | null;
  const body = record?.body;
  const at = record?.at;
  if (typeof body !== "string" || typeof at !== "number") {
    remove(storage, key);
    return null;
  }
  if (now - at > DRAFT_STORAGE_TTL_MS) {
    remove(storage, key);
    return null;
  }
  // An empty body is not a draft. Returning "" would be indistinguishable from
  // a real restore and would shadow a server draft the host DOES want shown.
  return body === "" ? null : body;
}

/** Persist the composer body. Writing an empty body clears the entry rather
 *  than storing one, so an emptied box leaves nothing behind to restore. */
export function writeStoredDraft(
  storage: DraftStorage | null,
  persistKey: string,
  body: string,
  now: number = Date.now(),
): void {
  if (!storage || !persistKey) return;
  const key = draftStorageKey(persistKey);
  if (body === "") {
    remove(storage, key);
    return;
  }
  try {
    storage.setItem(key, JSON.stringify({ body, at: now }));
  } catch {
    // Quota exceeded, or storage disabled mid-session. A dropped draft backup
    // is not worth breaking a keystroke over.
  }
}

export function clearStoredDraft(
  storage: DraftStorage | null,
  persistKey: string,
): void {
  if (!storage || !persistKey) return;
  remove(storage, draftStorageKey(persistKey));
}

function remove(storage: DraftStorage, key: string): void {
  try {
    storage.removeItem(key);
  } catch {
    // same as above — best effort
  }
}
