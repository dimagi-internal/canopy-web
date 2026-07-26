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
