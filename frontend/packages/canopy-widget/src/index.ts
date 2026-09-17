/**
 * `canopy-widget` — layer 3 of the embedded-agent SDK (v2 spec §1, §2, §3).
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
import {
  SOURCE,
  isFrameMessage,
  originOf,
  readCookie,
  type ActionSpec,
  type HostMessage,
} from './protocol'

export type { DisplayMode } from './chrome'
export type { ActionSpec } from './protocol'

/** `window.localStorage`, or null where reaching for it throws. */
function safeLocalStorage(): Pick<Storage, 'getItem' | 'setItem'> | null {
  try {
    const store = globalThis.localStorage
    // Present but broken is a real state (jsdom without a backing file, some
    // privacy extensions), and it fails at the first call rather than here.
    return typeof store?.getItem === 'function' ? store : null
  } catch {
    return null
  }
}

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
  /** Name of the host's CSRF cookie, when it uses Django's double-submit
   *  scheme. Defaults to Django's own default, `csrftoken`.
   *
   *  It has to be an option rather than a constant, because the name is not
   *  fixed: a Django app served under a path prefix on a SHARED host must
   *  rename the cookie or it collides with its siblings (canopy on labs uses
   *  `csrftoken_canopy`). With the wrong name the header is simply absent, the
   *  mint 403s, and the widget shows an unexplained failure to start. */
  csrfCookieName?: string
  /** Where a dragged launcher position is remembered. Defaults to
   *  `localStorage`; pass `null` to have the bubble start in its corner every
   *  time. */
  storage?: Pick<Storage, 'getItem' | 'setItem'> | null
  /** Called when canopy says a resource this page is showing has changed.
   *  The argument is the resource URI; the host re-reads however it likes. */
  onInvalidate?: (resource: string) => void
  /** Opaque metadata stamped on sessions this widget creates. */
  metadata?: Record<string, unknown>
  /** Text on the launcher bubble. The HOST names it, because the host's page
   *  is where it appears and only the host knows what its people call this
   *  thing — "Canopy AI" on canopy itself, something else on a partner site. */
  launcherLabel?: string
  /** Whether the launcher carries an × that hides it (default true).
   *
   *  The bubble is fixed to the corner of somebody else's page, and on a phone
   *  it lands on whatever is already in that corner. Somebody who wants the
   *  page rather than the agent needs a way to say so. Hosts that have laid
   *  out around the launcher can turn it off. */
  dismissible?: boolean
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
  /** Hide the launcher for the rest of this page load — what the × does.
   *  Exposed so a host can offer its own way to put it away. */
  dismiss(): void
  isDismissed(): boolean
  /** What the agent may read off this page. Pulled when a session opens, not
   *  subscribed to — see `canopy-client/bridge` for why. */
  provideContext(provider: ContextProvider): void
  /** Push what the page is showing now. Safe to call on every change. */
  setPageState(state: Record<string, unknown>): void
  /** Offer one named action.
   *
   *  `options.parameters` is JSON-Schema and should be supplied: without it the
   *  agent knows the action exists but not how to call it. Re-registering a
   *  name replaces it, so a re-rendering host can call this freely. */
  registerAction(name: string, action: HostAction, options?: Omit<ActionSpec, 'name'>): void
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
  // Replayed on init: a frame that mounts after the page declared its view must
  // not start blind, and the host has no way to know when the frame is ready.
  let lastState: Record<string, unknown> | null = null
  // The frame answers `ready` when its listener is up and it is about to be
  // handed a token. Nothing is pushed to it before that.
  let frameReady = false
  const actions = new Map<string, { fn: HostAction; spec: ActionSpec }>()
  let destroyed = false

  const chrome: Chrome = createChrome(src, {
    mode,
    target: options.target,
    launcherLabel: options.launcherLabel ?? 'Ask Canopy',
    dismissible: options.dismissible ?? true,
    app: options.app,
    // Resolved here, not inside the chrome: merely TOUCHING `localStorage` can
    // throw in a private window or where a host has blocked site data, so the
    // access is wrapped once and the chrome is handed a value or a null.
    storage: options.storage !== undefined ? options.storage : safeLocalStorage(),
    title: options.title ?? 'Canopy assistant',
    width: options.width ?? 400,
    zIndex: options.zIndex ?? 2147483000,
    onToggle: (open) => post({ source: SOURCE, type: 'visibility', open }),
  })

  /** Sorted so a host that registers in a different order across renders does
   *  not hand the agent a different-looking tool list each time. */
  function actionSpecs(): ActionSpec[] {
    return [...actions.values()].map((a) => a.spec).sort((x, y) => x.name.localeCompare(y.name))
  }

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
    const token = readCookie(document.cookie, options.csrfCookieName || 'csrftoken')
    return token ? { 'X-CSRFToken': token } : {}
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
      case 'invalidate': {
        // The host decides what re-reading means; the widget only relays that
        // something moved. An `onInvalidate` the host never set is not an
        // error — a host that does not refresh itself is a worse page, not a
        // broken one.
        options.onInvalidate?.(String(message.resource ?? ''))
        return
      }

      case 'ready': {
        frameReady = true
        try {
          post({
            source: SOURCE,
            type: 'init',
            token: await mintToken(),
            agent: options.agent,
            metadata: options.metadata,
            actions: actionSpecs(),
          })
          // After init, never inside it: `init` carries the token and is the
          // one message whose shape the frame validates strictly. The view is
          // a separate fact and rides its own frame.
          if (lastState) post({ source: SOURCE, type: 'state', state: lastState })
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
        const entry = actions.get(message.name)
        if (!entry) {
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
          const result = await entry.fn(message.args ?? {})
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
    dismiss: () => chrome.dismiss(),
    isDismissed: () => chrome.isDismissed(),
    provideContext(next) {
      provider = next
    },
    /** Push what the page is showing NOW.
     *
     *  Unlike `provideContext`, which is PULLED once when a conversation opens,
     *  this is pushed on every change — so a user who filters the page after
     *  opening the chat does not leave the agent holding a screen that has
     *  moved. The last value is replayed on `init`, so a panel opened later
     *  still starts from the current view rather than from nothing.
     */
    setPageState(next) {
      lastState = next
      // Only once the frame has handshaken. Before `ready` there is nothing
      // listening that could act on it, and the view describes the user's
      // screen — so it waits for the same moment the token does, and arrives
      // via the replay on init. Without this the first push races the
      // handshake and can precede it, which is both useless and looser than
      // it needs to be.
      if (frameReady) post({ source: SOURCE, type: 'state', state: next })
    },
    registerAction(name, action, options) {
      actions.set(name, {
        fn: action,
        spec: { name, description: options?.description, parameters: options?.parameters },
      })
      // Tell an already-open frame, so an action registered after mount becomes
      // callable without reopening the panel.
      post({ source: SOURCE, type: 'actions', actions: actionSpecs() })
    },
    unregisterAction(name) {
      actions.delete(name)
      post({ source: SOURCE, type: 'actions', actions: actionSpecs() })
    },
    destroy() {
      if (destroyed) return
      destroyed = true
      window.removeEventListener('message', listener)
      chrome.destroy()
    },
  }
}
