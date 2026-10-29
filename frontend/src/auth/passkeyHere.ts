/**
 * Where a passkey can be used — and where the person goes when it cannot.
 *
 * A passkey needs a real browser: WebAuthn in the page, and a session the person
 * signed into. The Canopy Runner desktop app hosts canopy in a web view that has
 * neither. WebKit refuses WebAuthn to an app without Apple's associated-domains
 * entitlement, and the app signs in with a session minted from the runner's token,
 * on which canopy refuses every step-up and approval (`apps/passkeys/stepup.py`).
 * So there, a passkey moment is HANDED to the person's browser: the approval's own
 * page opens there, they confirm with the passkey they already use, and the app's
 * page picks the result up from the server (`useWhileAway`).
 *
 * The contract for any host that frames canopy without passkeys (the macOS app,
 * `runner/menubar`, or a Windows one on WebView2): put `CanopyRunner/` in the user
 * agent, and send `window.open` to the system browser. Canopy also hears it from
 * `/api/me/` (`passkeys_here: false` on a token-minted session), which covers an
 * app built before it said so.
 */
import { useEffect, useRef } from 'react'

export const RUNNER_APP_MARKER = 'CanopyRunner/'

/** Why the button opens the browser, in one sentence, wherever it does. */
export const PASSKEY_IN_BROWSER =
  'Passkeys don’t work inside the Canopy Runner app, so this opens in your browser, where yours does. This window updates when you’re done.'

let sessionAllows = true

/** `/api/me/`'s `passkeys_here`, set once the app knows who is signed in. */
export function setSessionPasskeys(allowed: boolean): void {
  sessionAllows = allowed
}

export function inRunnerApp(ua: string | undefined = typeof navigator !== 'undefined' ? navigator.userAgent : ''): boolean {
  return (ua ?? '').includes(RUNNER_APP_MARKER)
}

/** Can a passkey ceremony run on this page? False means: hand it to the browser. */
export function passkeysWorkHere(): boolean {
  return sessionAllows && !inRunnerApp()
}

/** A ceremony whose result must stay on THIS page (registering a passkey, a vault
 *  key's PRF unlock) cannot be handed to the browser: refuse it in words instead. */
export class PasskeyInBrowserError extends Error {
  constructor() {
    super('Passkeys don’t work inside the Canopy Runner app. Open this page in your browser to do this.')
    this.name = 'PasskeyInBrowserError'
  }
}

export function requirePasskeysHere(): void {
  if (!passkeysWorkHere()) throw new PasskeyInBrowserError()
}

/** The absolute URL of `target`: a full URL as is, an app route (`/act-as/…`) under
 *  the deployment's prefix (`/canopy` on the shared labs host, `frameBase.ts`). */
export function browserUrl(target: string, base: string = import.meta.env.BASE_URL): string {
  if (/^https?:\/\//.test(target)) return target
  return new URL(base.replace(/\/$/, '') + target, window.location.origin).href
}

/** Open a canopy page in the person's own browser (a new tab when already in one). */
export function openInBrowser(target: string): void {
  window.open(browserUrl(target), '_blank', 'noopener')
}

/**
 * While `active`, re-read what the person is deciding in their browser: every few
 * seconds, and at once when this window comes back to the front. `refresh` is the
 * page's own read; it stops being called the moment `active` goes false.
 */
export function useWhileAway(refresh: () => unknown, active: boolean, everyMs = 3000): void {
  const latest = useRef(refresh)
  latest.current = refresh
  useEffect(() => {
    if (!active) return
    const tick = () => void latest.current()
    const timer = setInterval(tick, everyMs)
    window.addEventListener('focus', tick)
    return () => {
      clearInterval(timer)
      window.removeEventListener('focus', tick)
    }
  }, [active, everyMs])
}
