/**
 * `canopy.signOff` — a person approves one action on the host's own page, with their
 * passkey, in canopy's frame.
 *
 * The site's server asks canopy first (`canopy_sdk.sign_offs.request_sign_off`, or the
 * Django helper `canopy_sdk.django.sign_offs.request`) and hands the request's id to
 * the page. The page calls `canopy.signOff({ ..., id })`: a small dialog opens over it
 * holding canopy's `/embed/sign-off` frame — no launcher, no chat — where the person
 * reads what the site asks and approves with their passkey (or declines). The promise
 * then resolves with how it ended, and the dialog is gone.
 *
 * **The page learns the state, never the approval.** The signed approval stays in
 * canopy; the site's SERVER reads it (`canopy_sdk.django.sign_offs.status`) and checks
 * it before doing anything. A page that only believed this promise would be trusting
 * its own browser.
 *
 * The frame mints its token from the same host endpoint as the panel (`tokenUrl`),
 * with the same origin discipline: messages go to canopy's origin only, and are
 * accepted only from this dialog's own frame.
 */

import { mintToken, type MintOptions } from './mint'
import { SOURCE, isFrameMessage, isSignOffId, originOf, type HostMessage, type SignOffState } from './protocol'
import { normalizeTheme, type WidgetTheme } from './theme'

export interface SignOffOptions extends MintOptions {
  /** Where canopy lives, browser-facing (as for `init`). */
  baseUrl: string
  /** The registered Connected-site name: selects the framing policy canopy serves. */
  app: string
  /** An agent of the site's workspace, when the site name needs telling apart. */
  agent?: string
  /** The sign-off request's id (`so_…`), as canopy answered the site's server. */
  id: string
  /** As for `init`; `theme.mode` picks the frame's light or dark. */
  theme?: WidgetTheme
  zIndex?: number
  /** The dialog's accessible name. */
  title?: string
}

export interface SignOffOutcome {
  id: string
  /** `approved` / `declined` in the frame; `expired` when it was already over;
   *  `closed` when the person put the dialog away without deciding. */
  state: SignOffState
}

const FRAME_WIDTH = 420
const MIN_HEIGHT = 160
const MAX_HEIGHT = 640

export function signOff(options: SignOffOptions): Promise<SignOffOutcome> {
  if (!isSignOffId(options.id)) {
    return Promise.reject(new Error('canopy.signOff needs the sign-off id canopy gave the site (so_…)'))
  }
  const pageOrigin = typeof window !== 'undefined' ? window.location.origin : 'http://localhost'
  const canopyOrigin = originOf(options.baseUrl, pageOrigin)
  const base = options.baseUrl.replace(/\/$/, '')
  const theme = normalizeTheme(options.theme)
  const src =
    `${base}/embed/sign-off?app=${encodeURIComponent(options.app)}` +
    `&sign_off=${encodeURIComponent(options.id)}` +
    (options.agent ? `&agent=${encodeURIComponent(options.agent)}` : '') +
    (theme.mode ? `&theme=${theme.mode}` : '')

  return new Promise<SignOffOutcome>((resolve) => {
    const host = document.createElement('div')
    host.setAttribute('data-canopy-sign-off', options.id)
    const root = host.attachShadow({ mode: 'open' })
    const style = document.createElement('style')
    style.textContent = `
      :host { all: initial; }
      .backdrop { position: fixed; inset: 0; background: rgba(12, 10, 9, 0.45);
        display: grid; place-items: center; z-index: ${options.zIndex ?? 2147483000}; }
      .dialog { width: min(${FRAME_WIDTH}px, calc(100vw - 32px)); border-radius: 12px;
        overflow: hidden; box-shadow: 0 20px 50px rgba(0, 0, 0, 0.35); background: transparent; }
      iframe { display: block; width: 100%; height: ${MIN_HEIGHT * 2}px; border: 0; }
    `
    const backdrop = document.createElement('div')
    backdrop.className = 'backdrop'
    backdrop.setAttribute('part', 'sign-off-backdrop')
    const dialog = document.createElement('div')
    dialog.className = 'dialog'
    dialog.setAttribute('role', 'dialog')
    dialog.setAttribute('aria-modal', 'true')
    dialog.setAttribute('aria-label', options.title ?? 'Approve with your passkey')
    dialog.setAttribute('part', 'sign-off')
    const iframe = document.createElement('iframe')
    iframe.src = src
    iframe.title = options.title ?? 'Approve with your passkey'
    // As the panel's frame (chrome.ts), minus forms and popups it has no use for.
    iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin')
    // The passkey is canopy's: the page delegates the one feature that lets the frame ask.
    iframe.setAttribute('allow', 'publickey-credentials-get')
    dialog.appendChild(iframe)
    backdrop.appendChild(dialog)
    root.append(style, backdrop)
    document.body.appendChild(host)

    let done = false
    function finish(state: SignOffState): void {
      if (done) return
      done = true
      window.removeEventListener('message', listener)
      document.removeEventListener('keydown', onKey, true)
      host.remove()
      resolve({ id: options.id, state })
    }

    function post(message: HostMessage): void {
      // Explicit targetOrigin, never '*': the token goes to canopy and nowhere else.
      iframe.contentWindow?.postMessage(message, canopyOrigin)
    }

    async function onMessage(event: MessageEvent): Promise<void> {
      if (done || event.origin !== canopyOrigin || event.source !== iframe.contentWindow) return
      if (!isFrameMessage(event.data)) return
      const message = event.data
      switch (message.type) {
        case 'ready': {
          try {
            const minted = await mintToken(options)
            post({ source: SOURCE, type: 'init', token: minted.token, expires_at: minted.expires_at,
                   agent: options.agent, actions: [], theme })
          } catch (error) {
            post({ source: SOURCE, type: 'token-error', id: 'init',
                   message: error instanceof Error ? error.message : 'could not mint a token' })
          }
          return
        }
        case 'token-request': {
          try {
            const minted = await mintToken(options)
            post({ source: SOURCE, type: 'token', id: message.id, token: minted.token, expires_at: minted.expires_at })
          } catch (error) {
            post({ source: SOURCE, type: 'token-error', id: message.id,
                   message: error instanceof Error ? error.message : 'could not mint a token' })
          }
          return
        }
        case 'resize': {
          const height = Math.max(MIN_HEIGHT, Math.min(MAX_HEIGHT, Math.round(Number(message.height) || 0)))
          iframe.style.height = `${height}px`
          return
        }
        case 'sign-off': {
          // Only this dialog's request, and only a state the protocol names.
          if (message.id !== options.id) return
          if (['approved', 'declined', 'expired', 'closed'].includes(message.state)) finish(message.state)
          return
        }
        case 'close': {
          finish('closed')
          return
        }
      }
    }

    const listener = (event: MessageEvent) => void onMessage(event)
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') finish('closed')
    }
    window.addEventListener('message', listener)
    document.addEventListener('keydown', onKey, true)
    // A click on the backdrop (not the dialog) puts it away undecided.
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) finish('closed')
    })
  })
}
