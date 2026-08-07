/**
 * The Google OAuth entry URL, returning to `next` once the session is
 * established.
 *
 * Prefix-aware: `BASE_URL` is `/` at the root deployment and `/canopy/` as a
 * labs tenant, so the bounce stays under THIS deployment rather than landing on
 * a sibling tenant's login.
 *
 * It lives here because four call sites built this same string by hand — the
 * API client's 401 bounce, the full-page login card, the invite accept page,
 * and (canopy-web#516) the walkthrough viewer's anonymous branch. A prefix rule
 * copied five times is a prefix rule that drifts; copied once, it can't.
 */
export function loginHref(next: string): string {
  const base = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')
  return `${base}/accounts/google/login/?next=${encodeURIComponent(next)}`
}

/**
 * The current location as the `next` to come back to after signing in.
 *
 * Path + search deliberately, never the hash: the fragment never reaches the
 * server, so round-tripping it through `?next=` would be theatre. (A walkthrough
 * deep link like `#scene-3` is lost across the bounce either way — that is the
 * browser's contract, not ours.)
 */
export function currentNext(): string {
  return window.location.pathname + window.location.search
}
