/**
 * Where canopy's API is, as seen from inside the widget's iframe.
 *
 * The frame is served BY canopy, so its own origin is canopy's origin — but the
 * origin alone is not the base. canopy runs either at the root or under a path
 * prefix (`/canopy`, as a tenant on the shared labs host), and every backend
 * path has to carry that prefix: `frontend/src/api/base.ts` spells out the
 * consequence of dropping it — a bare `/api/...` resolves against the document
 * origin and hits whichever app owns the ROOT of that host, which on labs is
 * connect-labs, not canopy.
 *
 * Both consumers need it. `rest` concatenates paths onto this string, and
 * `buildSessionWsUrl` takes the same value and swaps its scheme — canopy's
 * socket is at `<prefix>/ws/canopy-sessions/…`, exactly as the SPA's own
 * `wsUrl()` builds it.
 *
 * Kept pure (both halves passed in) so it is testable without a DOM, which is
 * the only way to assert the prefixed case at all: a test runs at the root.
 */
export function frameBaseUrl(origin: string, basePath: string): string {
  return `${origin}${basePath.replace(/\/$/, '')}`
}

/** The live value: the frame's own origin plus the deployment prefix vite baked
 *  into this bundle. */
export function currentFrameBaseUrl(): string {
  return frameBaseUrl(window.location.origin, import.meta.env.BASE_URL)
}
