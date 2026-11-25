/**
 * Deployment base-path helpers.
 *
 * canopy-web runs either at the root ("/" — GCP / local) or under a path prefix
 * ("/canopy/" as a labs tenant on labs.connect.dimagi.com). Vite bakes the
 * prefix into `import.meta.env.BASE_URL`.
 *
 * The generated openapi-fetch client (`apiV2`) already carries this prefix via
 * its `baseUrl`. Hand-rolled raw-`fetch` clients do NOT — a literal
 * `fetch('/api/...')` resolves against the document origin, so under /canopy it
 * hits the ROOT tenant (connect-labs) and 404s. Those clients must wrap their
 * backend paths in `apiUrl()`.
 */

/** Strip a single trailing slash so a base concatenates cleanly with a path. */
export function normalizeBase(base: string): string {
  return base.replace(/\/$/, '')
}

/**
 * Prefix a root-relative backend path with a deployment base. At the root
 * deployment the base is "/" (→ "") and the path is unchanged; under /canopy
 * it becomes "/canopy/api/...". Kept pure (base passed in) so it's unit-testable
 * without stubbing `import.meta.env`.
 */
export function joinBase(base: string, path: string): string {
  return `${normalizeBase(base)}${path}`
}

/** The live deployment prefix ("" at root, "/canopy" as a labs tenant). */
export const API_BASE = normalizeBase(import.meta.env.BASE_URL)

/** Prefix a backend path (e.g. "/api/system/overview") with the live base. */
export function apiUrl(path: string): string {
  return joinBase(import.meta.env.BASE_URL, path)
}

/** Extract (and URL-decode) a named cookie from a cookie string. Pure, so the
 *  parsing is unit-testable without a DOM. */
export function readCookieFrom(cookieString: string, name: string): string {
  const m = cookieString.match(new RegExp(`(?:^|;\\s*)${name}=([^;]+)`))
  return m ? decodeURIComponent(m[1]) : ''
}

/**
 * Django's CSRF cookie name. Default "csrftoken"; the /canopy labs tenant
 * path-scopes it to "csrftoken_canopy" so it can't collide with the sibling
 * tenants on the shared host. Inlined at build time via Vite `define` (see
 * vite.config.ts). Reading the wrong name means writes ship no CSRF token → 403.
 */
export const CSRF_COOKIE_NAME: string = __CSRF_COOKIE_NAME__

/** The token `/api/csrf/` last said (`api/csrf.ts`), for a page that can't read its cookies. */
let toldToken = ''

export function rememberCsrfToken(token: string): void {
  toldToken = token
}

/** Read the CSRF token from its (deployment-specific) cookie; else the one `/api/csrf/` said.
 *  Inside the browser extension's side panel Chrome sends canopy's cookies but hides them from
 *  `document.cookie`, so there the cookie reads empty and only the told token works. */
export function getCsrfToken(): string {
  return readCookieFrom(document.cookie, CSRF_COOKIE_NAME) || toldToken
}
