/**
 * `@canopy/widget` — layer 3 of the embedded-agent SDK (v2 spec §1, §2, §3).
 *
 * One script tag, a launcher, and a canopy agent that can read the page it is
 * on. Framework-free and dependency-free on the host side: the chat UI runs
 * inside the iframe with its own bundled React, so the host's framework — or
 * absence of one — is irrelevant. That is what makes this usable on
 * connect-labs, which is React 18, Django templates, alpine and htmx.
 *
 * ## The credential path
 *
 * Issuance is unchanged from ace-web's integration: the HOST backend holds the
 * `AppCredential` and exchanges it. That cannot move into a browser, so "one
 * script tag" always means *plus one host endpoint*.
 *
 * What the origin boundary changes is DELIVERY. The token is never in the
 * iframe URL — a `#token=` fragment lands in `document.location`, history and
 * potentially a referrer. The host fetches it same-origin (cookie + CSRF,
 * untouched) and posts it in with an explicit `targetOrigin`.
 *
 * And REFRESH becomes a round trip: the frame has no cookies and cannot mint,
 * so it asks and the host answers.
 *
 * ## Origin discipline
 *
 * `targetOrigin` is the canopy origin derived from `baseUrl`, never `'*'`, on
 * every message. Inbound, a message is dropped unless `event.origin` is that
 * same origin AND `event.source` is our own iframe's window — origin alone
 * would accept a message from any other frame that happens to share it.
 * Messages are also filtered by KIND: the host issues `init`/`token`/…, so one
 * of those arriving *at* the host is impersonation, not input.
 *
 * ## The honest limit
 *
 * Context and actions run in the user's own session, which is what makes the
 * ACL story airtight — the agent can only see and do what this user could. It
 * also means both need the page open. Close the tab and the agent cannot read
 * or act; there is no server-side path. Action is scoped to the life of the
 * visit.
 */

import { createChrome, type Chrome, type DisplayMode } from './chrome'
import { SOURCE, isFrameMessage, originOf, type HostMessage } from './protocol'

export type { DisplayMode } from './chrome'

export interface CanopyWidgetOptions {
  /** Where canopy lives, browser-facing. Absolute for a cross-origin canopy
   *  (the usual embedded case), or a path prefix if same-origin. */
  baseUrl: string
  /** The registered `AppCredential` name — selects the framing policy canopy
   *  serves. It cannot widen anything: the browser enforces the
   *  `frame-ancestors` canopy returns for it. */
  app: string
  /** Host endpoint that mints a delegated token for the signed-in user.
   *  Must return `{ token, expires_at }`. Called same-origin with credentials,
   *  so the host's normal session auth applies. */
  tokenUrl: string
  mode?: DisplayMode
  /** Required for `inline`. */
  target?: string | Element
  /** Preselect an agent, skipping the picker. Must be one the app is allowed
   *  to offer AND the user can reach — canopy decides, not this. */
  agent?: string
  /** Opaque metadata stamped on sessions this widget creates. */
  metadata?: Record<string, unknown>
  launcherLabel?: string
  title?: string
  width?: number
  zIndex?: number
  /** Open immediately rather than waiting for the launcher. */
  open?: boolean
}

export type ContextProvider = () => unknown | Promise<unknown>
export type HostAction = (args: Record<string, unknown>) => unknown | Promise<unknown>

export interface CanopyWidget {
  open(): void
  close(): void
  toggle(): void
  isOpen(): boolean
  /** What the agent may read off this page. Pulled when a session opens, not
   *  subscribed to — see `@canopy/client/bridge` for why. */
  provideContext(provider: ContextProvider): void
  /** Offer one named action. Re-registering a name replaces it, so a
   *  re-rendering host can call this freely. */
  registerAction(name: string, action: HostAction): void
  unregisterAction(name: string): void
  /** Remove the widget and stop listening. Idempotent. */
  destroy(): void
}

interface TokenResponse {
  token: string
  expires_at?: string
}

export function init(options: CanopyWidgetOptions): CanopyWidget {
  const mode = options.mode ?? 'overlay'
  const pageOrigin =
    typeof window !== 'undefined' ? window.location.origin : 'http://localhost'
  const canopyOrigin = originOf(options.baseUrl, pageOrigin)

  const base = options.baseUrl.replace(/\/$/, '')
  const src = `${base}/embed/chat?app=${encodeURIComponent(options.app)}`

  let provider: ContextProvider | null = null
  const actions = new Map<string, HostAction>()
  let destroyed = false

  const chrome: Chrome = createChrome(src, {
    mode,
    target: options.target,
    launcherLabel: options.launcherLabel ?? 'Ask Canopy',
    title: options.title ?? 'Canopy assistant',
    width: options.width ?? 400,
    zIndex: options.zIndex ?? 2147483000,
    onToggle: (open) => post({ source: SOURCE, type: 'visibility', open }),
  })

  function post(message: HostMessage): void {
    // Explicit targetOrigin on every single message. '*' would broadcast the
    // token to whatever happens to be in the frame if the src were ever
    // redirected.
    chrome.iframe.contentWindow?.postMessage(message, canopyOrigin)
  }

  async function mintToken(): Promise<string> {
    const response = await fetch(options.tokenUrl, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', ...csrfHeader() },
      body: '{}',
    })
    if (!response.ok) {
      throw new Error(`token endpoint returned ${response.status}`)
    }
    const body = (await response.json()) as TokenResponse
    if (!body?.token) throw new Error('token endpoint returned no token')
    return body.token
  }

  /** Django's CSRF cookie, if the host uses one. Read rather than required, so
   *  a host with a different scheme (or none, for a `@csrf_exempt` mint) is not
   *  forced into ours. */
  function csrfHeader(): Record<string, string> {
    if (typeof document === 'undefined') return {}
    const match = /(?:^|;\s*)csrftoken=([^;]+)/.exec(document.cookie)
    return match ? { 'X-CSRFToken': decodeURIComponent(match[1]) } : {}
  }

  async function onMessage(event: MessageEvent): Promise<void> {
    if (destroyed) return
    // Both checks are required. Origin alone would accept a message from any
    // other frame served from canopy's origin; source alone would accept one
    // from our frame after a redirect elsewhere.
    if (event.origin !== canopyOrigin) return
    if (event.source !== chrome.iframe.contentWindow) return
    if (!isFrameMessage(event.data)) return

    const message = event.data
    switch (message.type) {
      case 'ready': {
        try {
          post({
            source: SOURCE,
            type: 'init',
            token: await mintToken(),
            agent: options.agent,
            metadata: options.metadata,
            actions: [...actions.keys()].sort(),
          })
        } catch (error) {
          // The frame is up but unusable. Tell it so it can say so, rather than
          // sit on a spinner the user cannot interpret.
          post({
            source: SOURCE,
            type: 'token-error',
            id: 'init',
            message: error instanceof Error ? error.message : 'could not mint a token',
          })
        }
        return
      }
      case 'token-request': {
        try {
          post({ source: SOURCE, type: 'token', id: message.id, token: await mintToken() })
        } catch (error) {
          post({
            source: SOURCE,
            type: 'token-error',
            id: message.id,
            message: error instanceof Error ? error.message : 'could not mint a token',
          })
        }
        return
      }
      case 'context-request': {
        // A host that lends no context is a legitimate host; `{}` rather than
        // an error.
        let context: Record<string, unknown> = {}
        try {
          context = ((await provider?.()) as Record<string, unknown>) ?? {}
        } catch {
          // A throwing provider must not take the conversation down with it.
          context = {}
        }
        post({ source: SOURCE, type: 'context', id: message.id, context })
        return
      }
      case 'action-request': {
        const action = actions.get(message.name)
        if (!action) {
          // Never a silent no-op: to an agent that is indistinguishable from
          // success, and it will carry on as though the page changed.
          post({
            source: SOURCE,
            type: 'action-error',
            id: message.id,
            message: `no host action named ${JSON.stringify(message.name)} is registered`,
          })
          return
        }
        try {
          const result = await action(message.args ?? {})
          post({ source: SOURCE, type: 'action-result', id: message.id, result })
        } catch (error) {
          post({
            source: SOURCE,
            type: 'action-error',
            id: message.id,
            message: error instanceof Error ? error.message : 'the host refused the action',
          })
        }
        return
      }
      case 'close': {
        chrome.close()
        return
      }
      case 'resize': {
        chrome.setHeight(message.height)
        return
      }
    }
  }

  const listener = (event: MessageEvent) => void onMessage(event)
  window.addEventListener('message', listener)

  if (options.open) chrome.open()

  return {
    open: () => chrome.open(),
    close: () => chrome.close(),
    toggle: () => chrome.toggle(),
    isOpen: () => chrome.isOpen(),
    provideContext(next) {
      provider = next
    },
    registerAction(name, action) {
      actions.set(name, action)
      // Tell an already-open frame, so an action registered after mount becomes
      // callable without reopening the panel.
      post({ source: SOURCE, type: 'actions', actions: [...actions.keys()].sort() })
    },
    unregisterAction(name) {
      actions.delete(name)
      post({ source: SOURCE, type: 'actions', actions: [...actions.keys()].sort() })
    },
    destroy() {
      if (destroyed) return
      destroyed = true
      window.removeEventListener('message', listener)
      chrome.destroy()
    },
  }
}

export default { init }
