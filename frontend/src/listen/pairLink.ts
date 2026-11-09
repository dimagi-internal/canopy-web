/**
 * The pairing link a laptop's QR code carries: /listen#pair=<base64url JSON {r, k, s, n, x}>.
 *
 * The fragment holds a one-time secret and never reaches the server. It is taken out of
 * the address bar at once — before the app can send a signed-out phone to sign in, which
 * would drop it — and kept in this tab's sessionStorage only until the page reads it.
 */
import { unb64u, type Bytes } from './relayCrypto'

export interface PairInvite {
  /** runner id */
  runnerId: string
  /** the laptop's static public key */
  laptopPub: string
  /** the 32-byte one-time secret */
  secret: Bytes
  laptopName: string
  /** unix seconds */
  expiresAt: number
}

const STASH = 'canopy.listen.pair'

/** The invite in a `#pair=` fragment, or null when it isn't one we can read. */
export function parsePairFragment(hash: string): PairInvite | null {
  const m = /^#?pair=([A-Za-z0-9_-]+)$/.exec(hash.trim())
  if (!m) return null
  try {
    const json: unknown = JSON.parse(new TextDecoder().decode(unb64u(m[1])))
    if (!json || typeof json !== 'object') return null
    const { r, k, s, n, x } = json as Record<string, unknown>
    if (typeof r !== 'string' || typeof k !== 'string' || typeof s !== 'string' || typeof x !== 'number') return null
    const secret = unb64u(s)
    const pub = unb64u(k)
    if (secret.length !== 32 || pub.length !== 65 || pub[0] !== 4) return null
    return { runnerId: r, laptopPub: k, secret, laptopName: typeof n === 'string' && n ? n : 'your laptop', expiresAt: x }
  } catch {
    return null
  }
}

export function isExpired(invite: PairInvite, nowMs = Date.now()): boolean {
  return invite.expiresAt * 1000 <= nowMs
}

function listenPath(pathname: string, base: string): boolean {
  const b = base.replace(/\/$/, '')
  return pathname.slice(b.length).replace(/\/$/, '') === '/listen'
}

/**
 * Called first thing at startup (main.tsx): on /listen with a `#pair=` fragment, move it
 * into sessionStorage and strip it from the address, so it is in no history entry, no
 * screenshot of the URL bar, and survives a sign-in round trip in this tab.
 */
export function captureListenPairing(win: Window = window, base = import.meta.env.BASE_URL): void {
  try {
    const { pathname, search, hash } = win.location
    if (!listenPath(pathname, base) || !hash.startsWith('#pair=')) return
    win.sessionStorage.setItem(STASH, hash)
    win.history.replaceState(win.history.state, '', pathname + search)
  } catch {
    // No storage (a private window that refuses it): the page reads nothing and says how to retry.
  }
}

/** The stashed fragment, once: reading it removes it. */
export function takeStashedPairing(win: Window = window): string | null {
  try {
    const value = win.sessionStorage.getItem(STASH)
    win.sessionStorage.removeItem(STASH)
    return value
  } catch {
    return null
  }
}
