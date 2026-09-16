/**
 * The frame's side of the host bridge.
 *
 * Inside the iframe there are no cookies for the host, no way to call the
 * host's endpoints, and no way to read its DOM. Everything the frame needs from
 * the page it is embedded in — a token, the page's state, an action — arrives
 * over `postMessage`. This module is the only place that speaks it.
 *
 * **Origin validation is not optional here even though framing is already
 * gated.** The shell is served with `frame-ancestors`, so only a registered
 * origin can frame it — but that constrains who may *embed* the frame, not who
 * may *post to* it. Any window with a handle to this one can post; so a message
 * is accepted only from an origin the SERVER listed in `window.CANOPY_EMBED`.
 * The frame never trusts a list the host sent it, because the host is the party
 * being authenticated.
 *
 * The one message posted before any of that is known is `ready`, which carries
 * nothing. It goes to `'*'` because the frame cannot know its parent's origin
 * until the parent speaks, and "I exist" is not a secret. Everything after is
 * replied to `event.origin` — an origin already checked against the list.
 */

export interface EmbedBootstrap {
  app: string
  /** Origins permitted to frame (and therefore to talk to) this frame. */
  origins: string[]
}

/** One thing the host page can be asked to do. `parameters` is JSON-Schema —
 *  without it the agent knows the action exists but not how to call it. */
export interface ActionSpec {
  name: string
  description?: string
  parameters?: Record<string, unknown>
}

export interface HostInit {
  token: string
  agent?: string
  metadata?: Record<string, unknown>
  actions: ActionSpec[]
}

const SOURCE = 'canopy-widget'

type Pending = { resolve: (v: never) => void; reject: (e: Error) => void }

export interface HostLink {
  /** Resolves when the host answers `ready` with a token. Rejects if the host
   *  says minting failed, so the UI can show why instead of spinning. */
  waitForInit(): Promise<HostInit>
  /** Ask the host to mint a fresh token — the frame cannot, having no cookies
   *  for the host's origin. This is what `@canopy/client`'s `fetchToken` becomes. */
  requestToken(): Promise<{ token: string; expiresAt: string }>
  /** The host page's current state, as the host chose to expose it. */
  requestContext(): Promise<Record<string, unknown>>
  /** Run one of the host's registered actions. Rejects on refusal. */
  runAction(name: string, args?: Record<string, unknown>): Promise<unknown>
  /** What the host currently offers, with schemas; updates as it registers. */
  actions(): ActionSpec[]
  onActionsChanged(listener: (specs: ActionSpec[]) => void): () => void
  /** What the host page is currently SHOWING, as last pushed.
   *
   *  `null` means the host has never pushed — which is NOT the same as an empty
   *  view, and the difference matters: a host that does not speak state at all
   *  should leave the agent's page state untouched rather than declaring the
   *  user's screen blank. */
  pageState(): Record<string, unknown> | null
  onPageStateChanged(listener: (state: Record<string, unknown>) => void): () => void
  /** Tell the host that a resource its page is showing has changed. */
  invalidate(resource: string): void
  /** Ask the host to close the panel (our own close button). */
  requestClose(): void
  /** Ask for a panel height, in the modes where the host owns it. */
  requestHeight(px: number): void
  /** Stop listening. The frame normally lives as long as its document, so
   *  nothing in the app calls this — but a link that cannot be torn down
   *  cannot be tested in isolation either, and an untestable listener is how
   *  the cross-link id collision above went unnoticed. */
  dispose(): void
}

export function createHostLink(bootstrap: EmbedBootstrap): HostLink {
  const allowed = new Set(bootstrap.origins)
  const pending = new Map<string, Pending>()
  let seq = 0
  // Request ids are namespaced per LINK, not just per request. They were
  // `f1`, `f2`… which collide across two links on one page — and since every
  // link hears every message on this window, one reply then settled the
  // pending entry in ALL of them. Production has exactly one frame, so it
  // could not bite there; it surfaced as a reply resolving two promises in
  // tests, which is the same defect with a witness.
  const linkId = Math.random().toString(36).slice(2, 8)

  let initResolve: ((v: HostInit) => void) | null = null
  let initReject: ((e: Error) => void) | null = null
  const initPromise = new Promise<HostInit>((resolve, reject) => {
    initResolve = resolve
    initReject = reject
  })

  let actionSpecs: ActionSpec[] = []
  const actionListeners = new Set<(specs: ActionSpec[]) => void>()
  // `null` until the host speaks, deliberately distinct from `{}`: a host that
  // never pushes must not be read as one declaring an empty screen.
  let latestState: Record<string, unknown> | null = null
  const stateListeners = new Set<(state: Record<string, unknown>) => void>()

  /** The parent's origin, learned from the first accepted message. Until then
   *  there is nothing to reply to — every outbound message except `ready` is a
   *  RESPONSE, so this is always set by the time one is sent. */
  let hostOrigin: string | null = null

  function send(message: Record<string, unknown>): void {
    if (!hostOrigin) return
    window.parent.postMessage({ source: SOURCE, ...message }, hostOrigin)
  }

  function nextId(): string {
    seq += 1
    return `${linkId}-${seq}`
  }

  function ask<T>(message: Record<string, unknown>): Promise<T> {
    const id = nextId()
    return new Promise<T>((resolve, reject) => {
      pending.set(id, { resolve: resolve as never, reject })
      if (!hostOrigin) {
        pending.delete(id)
        reject(new Error('the host has not spoken yet'))
        return
      }
      window.parent.postMessage({ source: SOURCE, ...message, id }, hostOrigin)
    })
  }

  function settle(id: string, kind: 'resolve' | 'reject', value: unknown): void {
    const entry = pending.get(id)
    if (!entry) return
    pending.delete(id)
    if (kind === 'resolve') entry.resolve(value as never)
    else entry.reject(value instanceof Error ? value : new Error(String(value)))
  }

  const onMessage = (event: MessageEvent) => {
    // The list comes from the SERVER (window.CANOPY_EMBED), never from a
    // message — the sender is the party being authenticated.
    if (!allowed.has(event.origin)) return
    const data = event.data as Record<string, unknown> | null
    if (!data || typeof data !== 'object' || data.source !== SOURCE) return
    // Only kinds the HOST sends. `ready`/`context-request`/… are ours; one
    // arriving here means something is replaying our own traffic at us.
    const type = data.type
    if (typeof type !== 'string') return

    if (hostOrigin === null) hostOrigin = event.origin

    switch (type) {
      case 'init':
        initResolve?.({
          token: String(data.token ?? ''),
          agent: data.agent ? String(data.agent) : undefined,
          metadata: (data.metadata as Record<string, unknown> | undefined) ?? {},
          actions: Array.isArray(data.actions) ? (data.actions as ActionSpec[]) : [],
        })
        actionSpecs = Array.isArray(data.actions) ? (data.actions as ActionSpec[]) : []
        return
      case 'token':
        settle(String(data.id), 'resolve', {
          token: String(data.token ?? ''),
          // The host's mint response carries an expiry, but the frame does not
          // need it to be accurate: @canopy/client refetches on 401 regardless,
          // and an unparseable value is treated as already-expired there. A far
          // future default would be the unsafe direction, so this is empty.
          expiresAt: String(data.expires_at ?? ''),
        })
        return
      case 'token-error':
        // `init` is the id the host uses for a failure during the handshake
        // itself, where there is no request to settle — surface it as the init
        // failing, so the UI can say why rather than spin.
        if (data.id === 'init') initReject?.(new Error(String(data.message ?? 'token failed')))
        else settle(String(data.id), 'reject', new Error(String(data.message ?? 'token failed')))
        return
      case 'context':
        settle(String(data.id), 'resolve', data.context ?? {})
        return
      case 'action-result':
        settle(String(data.id), 'resolve', data.result)
        return
      case 'action-error':
        settle(String(data.id), 'reject', new Error(String(data.message ?? 'action refused')))
        return
      case 'actions':
        actionSpecs = Array.isArray(data.actions) ? (data.actions as ActionSpec[]) : []
        actionListeners.forEach((l) => l(actionSpecs))
        return
      case 'state': {
        // A non-object is dropped rather than forwarded: the server would
        // refuse it anyway, and blanking a good view on a malformed push is
        // strictly worse than ignoring the push.
        const next = data.state
        if (!next || typeof next !== 'object' || Array.isArray(next)) return
        latestState = next as Record<string, unknown>
        stateListeners.forEach((l) => l(latestState as Record<string, unknown>))
        return
      }
      default:
        return
    }
  }

  window.addEventListener('message', onMessage)

  // "I exist". The only message sent before an origin is known, and the only
  // one carrying nothing — see the module docstring.
  window.parent.postMessage({ source: SOURCE, type: 'ready' }, '*')

  return {
    waitForInit: () => initPromise,
    requestToken: () => ask<{ token: string; expiresAt: string }>({ type: 'token-request' }),
    requestContext: () => ask<Record<string, unknown>>({ type: 'context-request' }),
    runAction: (name, args) => ask({ type: 'action-request', name, args: args ?? {} }),
    actions: () => actionSpecs,
    onActionsChanged(listener) {
      actionListeners.add(listener)
      return () => actionListeners.delete(listener)
    },
    pageState: () => latestState,
    onPageStateChanged(listener) {
      stateListeners.add(listener)
      return () => stateListeners.delete(listener)
    },
    invalidate: (resource: string) => send({ type: 'invalidate', resource }),
    requestClose: () => send({ type: 'close' }),
    requestHeight: (px) => send({ type: 'resize', height: px }),
    dispose() {
      window.removeEventListener('message', onMessage)
      // Anything still outstanding will never be answered now; rejecting is
      // honest, and leaving them pending forever would hang a caller.
      pending.forEach((entry) => entry.reject(new Error('the host link was disposed')))
      pending.clear()
    },
  }
}
