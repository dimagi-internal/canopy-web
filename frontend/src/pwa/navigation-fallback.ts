/**
 * Service-worker navigate-fallback ownership — the fail-safe rule.
 *
 * A same-origin *navigation* (including every `<iframe src>` load; a `<video
 * src>` load is NOT one) is answered by the SW from the precached SPA shell
 * (`index.html`) ONLY when its path matches an ALLOWLISTed SPA route prefix and
 * does NOT match a DENYLISTed server route. Everything else goes to the network
 * and reaches Django.
 *
 * This inverts the historical "shell for everything except a denylist of server
 * prefixes" default, which silently swallowed any server route nobody
 * remembered to denylist (issue #345: `/walkthrough/<id>/content` embedded via
 * `<iframe>` rendered the whole SPA again inside itself). The rule now FAILS
 * SAFE:
 *
 *   - A new SERVER route is safe by construction — unknown ⇒ network.
 *   - A forgotten SPA route only loses *offline* shell fallback; online it still
 *     resolves, because Django's catch-all `spa_view` serves `index.html` for
 *     any unknown non-API path.
 *
 * Workbox applies both lists: a navigation gets the shell iff it matches the
 * allowlist AND does not match the denylist (denylist wins). That precedence is
 * what lets a broad `walkthrough/` entry sit in the allowlist (so the viewer
 * shell `/walkthrough/<uuid>` stays offline-resilient) while the server stream
 * `/walkthrough/<uuid>/content` is carved back out on the denylist.
 *
 * Every pattern tolerates the optional `(canopy/)?` segment so it matches both
 * the root deployment (`/…`) and the labs tenant mount (`/canopy/…`). Workbox
 * tests these against `pathname + search`, so the content carve-outs below end
 * in `(?:\?.*)?$` — otherwise a token'd stream (`…/content?t=<share_token>`,
 * which the DDD console embeds now carry) would slip past the `content$` anchor
 * and be swallowed by the shell.
 *
 * When you add a top-level SPA route to `router.tsx`, add its prefix to the
 * allowlist (else it just misses offline support). When you add a server route,
 * you need do nothing — it is already excluded by default; add it to the
 * denylist only if it lives *under* an allowlisted SPA prefix (like the
 * `/content` streams below).
 */

/** Known SPA route prefixes — the only navigations that get the cached shell. */
export const NAVIGATE_FALLBACK_ALLOWLIST: RegExp[] = [
  /^\/(canopy\/)?$/, // root → workbench redirect
  /^\/(canopy\/)?w\//, // tenant-scoped surfaces (/w/:workspace/…)
  /^\/(canopy\/)?supervisor/,
  /^\/(canopy\/)?insights/,
  /^\/(canopy\/)?system/,
  /^\/(canopy\/)?settings/,
  /^\/(canopy\/)?sessions/,
  /^\/(canopy\/)?schedules/,
  /^\/(canopy\/)?activity/,
  /^\/(canopy\/)?timeline/, // legacy flat → tenant redirect (still an SPA render)
  /^\/(canopy\/)?shareouts/,
  /^\/(canopy\/)?walkthroughs/, // list page (plural); NOT the /walkthrough/ viewer
  /^\/(canopy\/)?agents/,
  /^\/(canopy\/)?ddd-release\//, // public release page (chrome-less)
  /^\/(canopy\/)?ddd-plans/,
  /^\/(canopy\/)?ddd/, // NB: after ddd-release/ddd-plans so those match first
  /^\/(canopy\/)?reviews/, // legacy flat → redirect
  /^\/(canopy\/)?review\//, // /review/:id surface
  /^\/(canopy\/)?walkthrough\//, // /walkthrough/:id VIEWER shell (…/content denied below)
  /^\/(canopy\/)?share\//, // /share/:token public viewer
  /^\/(canopy\/)?invite\//, // /invite/:token accept page
]

/**
 * Server-owned routes that must reach the network, never the shell. The first
 * six are Django's own prefixes; the last two are the content streams that
 * overlap an allowlisted SPA prefix and so must be carved back out here (the
 * denylist wins over the allowlist).
 */
export const NAVIGATE_FALLBACK_DENYLIST: RegExp[] = [
  /^\/(canopy\/)?api\//,
  /^\/(canopy\/)?accounts\//,
  /^\/(canopy\/)?admin\//,
  /^\/(canopy\/)?static\//,
  /^\/(canopy\/)?auth\//,
  /^\/(canopy\/)?health\/?$/,
  /^\/(canopy\/)?walkthrough\/.*\/content(?:\?.*)?$/, // streamed artifact bytes (Django)
  /^\/(canopy\/)?w\/.*\/content(?:\?.*)?$/, // legacy /w/<uuid>/content redirect (Django)
]

/**
 * Mirror of workbox's navigation-fallback decision, for tests: the SPA shell is
 * served iff the path matches the allowlist and does not match the denylist.
 */
export function shouldServeShell(path: string): boolean {
  const allowed = NAVIGATE_FALLBACK_ALLOWLIST.some((re) => re.test(path))
  const denied = NAVIGATE_FALLBACK_DENYLIST.some((re) => re.test(path))
  return allowed && !denied
}
