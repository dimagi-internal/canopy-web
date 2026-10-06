/**
 * The session WebSocket URL.
 *
 * Ported from ace-web's `frontend/src/canopy/ws.ts`. The only change is that the
 * token is passed in rather than read from a module global, so this is a pure
 * function and testable without a token store.
 *
 * `base` is one of two shapes:
 *   - a same-origin PATH (`/canopy` — a vite proxy in dev, or a shared ALB path
 *     prefix in prod) with no scheme or host of its own;
 *   - an absolute `http(s)://host[/path]` URL (canopy on a different host, which
 *     is the normal case for an embedded widget).
 *
 * `WebSocket` needs an absolute `ws(s)://` URL either way, so a bare path
 * borrows the current location's scheme and host; an absolute base keeps its own
 * and only has its scheme swapped.
 *
 * Open the socket with `buildSessionWsUrlWithTicket`. A WebSocket handshake
 * cannot carry an `Authorization` header, so the token used to ride as
 * `?token=` — and URLs land in access logs. canopy stopped reading a token from
 * the URL on 2026-10-06; it accepts a one-time ticket there instead.
 */

export interface WsLocation {
  protocol: string
  host: string
}

/** `location` is injectable so this works in a worker, in a test, and in an
 *  iframe — anywhere `window` may be absent or not the one you mean.
 *
 *  @deprecated canopy no longer reads a token from the socket URL (2026-10-06),
 *  so a socket opened with this URL is refused. Use
 *  `buildSessionWsUrlWithTicket`, or the client's `sessionSocketTicketUrl`. */
export function buildSessionWsUrl(
  base: string,
  sessionId: string,
  token: string | null,
  location?: WsLocation,
): string {
  return sessionUrl(base, sessionId, token ? `?token=${encodeURIComponent(token)}` : '', location)
}

/** The session socket URL carrying a one-time TICKET instead of the token.
 *
 *  Prefer this: a URL is written to access logs, and a ticket found in one is
 *  already spent. Mint the ticket with `POST /api/embed/ws-ticket` (a site's
 *  delegated token) or `POST /api/contact/ws-ticket` (a contact), presenting the
 *  token as a Bearer header, and fetch a fresh one for every connection —
 *  reconnects included, since each works once. */
export function buildSessionWsUrlWithTicket(
  base: string,
  sessionId: string,
  ticket: string,
  location?: WsLocation,
): string {
  return sessionUrl(base, sessionId, `?ticket=${encodeURIComponent(ticket)}`, location)
}

function sessionUrl(base: string, sessionId: string, query: string, location?: WsLocation): string {
  const isAbsolute = /^https?:\/\//i.test(base)

  let origin: string
  let pathPrefix: string

  if (isAbsolute) {
    const url = new URL(base)
    origin = `${url.protocol === 'https:' ? 'wss:' : 'ws:'}//${url.host}`
    pathPrefix = url.pathname.replace(/\/$/, '')
  } else {
    const loc =
      location ??
      (typeof window !== 'undefined'
        ? { protocol: window.location.protocol, host: window.location.host }
        : { protocol: 'http:', host: 'localhost' })
    origin = `${loc.protocol === 'https:' ? 'wss:' : 'ws:'}//${loc.host}`
    pathPrefix = base.replace(/\/$/, '')
  }

  // No token means no session has been minted yet, in which case the caller
  // should not be opening a socket. Left as a tokenless URL rather than thrown,
  // matching ace-web: the connect will fail loudly at the server instead of
  // turning a race into an exception in a render path.
  return `${origin}${pathPrefix}/ws/canopy-sessions/${encodeURIComponent(sessionId)}/${query}`
}
