/**
 * CSRF bootstrap.
 *
 * Hits the bare Django view at /api/csrf/ to set the csrftoken cookie
 * before the first POST/PATCH/DELETE. NOT a Ninja v2 endpoint — this
 * is intentionally out-of-band because it sets a cookie via Django's
 * @ensure_csrf_cookie decorator, which doesn't fit the Ninja pattern.
 */
import { apiUrl, rememberCsrfToken } from './base'

/** Also keeps the token the view says: a page that can't read its cookies (canopy inside the
 *  browser extension's side panel) sends that one. Run again after a sign-in, which changes it. */
export async function bootstrapCsrf(): Promise<void> {
  const res = await fetch(apiUrl('/api/csrf/'), { credentials: 'same-origin' })
  try {
    const body = (await res.json()) as { token?: unknown }
    if (typeof body.token === 'string') rememberCsrfToken(body.token)
  } catch {
    // an older server's body without a token: the cookie is all there is
  }
}
