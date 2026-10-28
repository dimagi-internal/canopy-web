/**
 * Minting the frame's token at the host — shared by the panel (`init`) and a sign-off
 * frame (`signOff`), which mint the same delegated token from the same host endpoint.
 */

import { readCookie } from './protocol'

export interface MintOptions {
  /** See `CanopyWidgetOptions.tokenUrl`. */
  tokenUrl: string | (() => string)
  /** See `CanopyWidgetOptions.csrfCookieName`. */
  csrfCookieName?: string
  /** See `CanopyWidgetOptions.csrfToken`. */
  csrfToken?: string | (() => string | null | undefined)
}

interface TokenResponse {
  token: string
  expires_at?: string
}

/** Mint a token at the host. `grant` asks for an act-as-you permission with it —
 *  only ever because the frame says the person has sent a message in the panel. */
export async function mintToken(options: MintOptions, grant = false): Promise<{ token: string; expires_at: string }> {
  const url = typeof options.tokenUrl === 'function' ? options.tokenUrl() : options.tokenUrl
  const response = await fetch(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...csrfHeader(options) },
    body: grant ? '{"grant":true}' : '{}',
  })
  if (!response.ok) {
    throw new Error(`token endpoint returned ${response.status}`)
  }
  const body = (await response.json()) as TokenResponse
  if (!body?.token) throw new Error('token endpoint returned no token')
  return { token: body.token, expires_at: typeof body.expires_at === 'string' ? body.expires_at : '' }
}

/** The host's CSRF token, if it has one. Read rather than required, so a host
 *  with a different scheme (or none, for a `@csrf_exempt` mint) is not forced
 *  into ours.
 *
 *  `csrfToken` is consulted first and wins, because a host that passes it has
 *  said the cookie is not readable — falling back to the cookie there would
 *  send nothing while looking like it tried. It is read on every mint, not
 *  captured once, so a rotated token (Django rotates on login) is picked up.
 *  A throwing provider is treated as "no token" rather than being allowed to
 *  break the mint: the 403 that follows names the real problem, an exception
 *  here would not. */
export function csrfHeader(options: MintOptions): Record<string, string> {
  const supplied = options.csrfToken
  if (supplied !== undefined) {
    let token: string | null | undefined
    try {
      token = typeof supplied === 'function' ? supplied() : supplied
    } catch {
      token = null
    }
    return token ? { 'X-CSRFToken': token } : {}
  }
  if (typeof document === 'undefined') return {}
  const token = readCookie(document.cookie, options.csrfCookieName || 'csrftoken')
  return token ? { 'X-CSRFToken': token } : {}
}
