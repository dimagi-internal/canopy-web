// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { init } from './index'
import { SOURCE, originOf } from './protocol'

const CANOPY = 'https://canopy.example.com'
const TOKEN_URL = '/labs/canopy/token'

/**
 * The frame is cross-origin in reality, so the tests drive it the way a real
 * frame would: by dispatching `message` events with an origin and a source, and
 * reading what the host posted back. The frame window is a stand-in (see
 * below), which is what the host checks `event.source` against.
 */
/** A storage that works, because jsdom's does not reliably here — the widget
 *  takes one rather than reaching for `window.localStorage`, which is what
 *  makes any of this testable. */
function memoryStorage(seed: Record<string, string> = {}) {
  const data = { ...seed }
  return {
    getItem: (k: string) => data[k] ?? null,
    setItem: (k: string, v: string) => {
      data[k] = v
    },
    data,
  }
}

function widgetHarness(overrides: Record<string, unknown> = {}) {
  const posted: Array<{ message: Record<string, unknown>; targetOrigin: string }> = []

  const widget = init({
    baseUrl: CANOPY,
    app: 'connect-labs',
    tokenUrl: TOKEN_URL,
    // Every harness gets its OWN storage. Falling through to the ambient
    // `window.localStorage` made the suite environment-dependent: this
    // machine's jsdom has no working one (so nothing persisted and every
    // test started clean) while CI's does, so one test's saved position
    // restored itself into the next test's fresh widget and marked it
    // moved before anything was dragged. Passing one here means the file
    // behaves the same wherever it runs; tests that care override it.
    storage: memoryStorage(),
    ...overrides,
  } as Parameters<typeof init>[0])

  const host = document.querySelector('[data-canopy-widget]') as HTMLElement
  const root = host.shadowRoot!
  const iframe = root.querySelector('iframe') as HTMLIFrameElement

  // jsdom gives an iframe inside a shadow root no browsing context, so
  // `contentWindow` is null and there is nothing real to post to. Stand in a
  // fake frame window: it captures what the host sends, and — because the host
  // checks `event.source` against exactly this object — it is also what lets
  // the origin/source discipline be tested at all.
  const frameWindow = {
    postMessage: (message: Record<string, unknown>, targetOrigin: string) => {
      posted.push({ message, targetOrigin })
    },
  }
  Object.defineProperty(iframe, 'contentWindow', { value: frameWindow, configurable: true })

  /** Speak as the frame. */
  async function fromFrame(data: unknown, opts: { origin?: string; source?: unknown } = {}) {
    window.dispatchEvent(
      new MessageEvent('message', {
        data,
        origin: opts.origin ?? CANOPY,
        source: (opts.source === undefined ? frameWindow : opts.source) as Window,
      }),
    )
    // Let the host's async handler settle (it awaits fetch / the provider).
    await new Promise((r) => setTimeout(r, 0))
  }

  const sent = (type: string) => posted.filter((p) => p.message.type === type)

  return { widget, host, root, iframe, posted, sent, fromFrame }
}

beforeEach(() => {
  document.body.innerHTML = ''
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ token: 'delegated-tok', expires_at: 'later' }),
    })),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('mounting and the three display modes', () => {
  it('overlay adds a launcher and a closed panel, leaving host layout alone', () => {
    const { root } = widgetHarness({ mode: 'overlay' })
    expect(root.querySelector('.launcher')).not.toBeNull()
    expect((root.querySelector('.panel') as HTMLElement).dataset.open).toBe('false')
  })

  it('docked positions a full-height rail', () => {
    const { root } = widgetHarness({ mode: 'docked' })
    expect((root.querySelector('.panel') as HTMLElement).dataset.mode).toBe('docked')
    expect(root.querySelector('.launcher')).not.toBeNull()
  })

  it('inline fills a host element, with no launcher and already open', () => {
    const slot = document.createElement('div')
    slot.id = 'slot'
    document.body.appendChild(slot)

    const { root, host } = widgetHarness({ mode: 'inline', target: '#slot' })

    expect(host.parentElement).toBe(slot)
    expect(root.querySelector('.launcher')).toBeNull()
    expect((root.querySelector('.panel') as HTMLElement).dataset.open).toBe('true')
  })

  it('inline refuses to guess when the host named no target', () => {
    expect(() => widgetHarness({ mode: 'inline' })).toThrow(/needs a `target`/)
  })

  it('inline says which selector missed rather than mounting nowhere', () => {
    expect(() => widgetHarness({ mode: 'inline', target: '#nope' })).toThrow(/#nope/)
  })

  it('positions its host so the z-index takes effect over a host page\'s own layers', () => {
    const { host } = widgetHarness({ mode: 'overlay' })
    expect(host.style.zIndex).toBe('2147483000')
    expect(host.style.position).toBe('relative')
  })

  it('puts its chrome in a shadow root so host CSS cannot deform it', () => {
    const { host } = widgetHarness()
    expect(host.shadowRoot).not.toBeNull()
    // Nothing of ours in the host's own tree to be styled by the host.
    expect(document.querySelector('.launcher')).toBeNull()
  })

  it('points the frame at the embed shell for this app', () => {
    const { iframe } = widgetHarness()
    expect(iframe.src).toBe(`${CANOPY}/embed/chat?app=connect-labs`)
  })

  it('names the agent on the shell URL, since a site name is unique only per tenant', () => {
    const { iframe } = widgetHarness({ agent: 'labs-helper' })
    expect(iframe.src).toBe(`${CANOPY}/embed/chat?app=connect-labs&agent=labs-helper`)
  })

  it('sandboxes the frame without letting it navigate the host page', () => {
    const { iframe } = widgetHarness()
    const sandbox = iframe.getAttribute('sandbox') ?? ''
    expect(sandbox).toContain('allow-scripts')
    expect(sandbox).toContain('allow-same-origin')
    expect(sandbox).not.toContain('allow-top-navigation')
    // A tab the panel opens is a normal page, not a copy of this sandbox: a sign-in
    // redirect opened from a card rendered blank without it (2026-10-09).
    expect(sandbox).toContain('allow-popups-to-escape-sandbox')
  })
})

describe('open/close', () => {
  it('the launcher toggles the panel and tracks aria-expanded', () => {
    const { root } = widgetHarness()
    const launcher = root.querySelector('.launcher') as HTMLButtonElement
    const panel = root.querySelector('.panel') as HTMLElement

    launcher.click()
    expect(panel.dataset.open).toBe('true')
    expect(launcher.getAttribute('aria-expanded')).toBe('true')

    launcher.click()
    expect(panel.dataset.open).toBe('false')
    expect(launcher.getAttribute('aria-expanded')).toBe('false')
  })

  it('inline cannot be closed — there would be no way back', () => {
    const slot = document.createElement('div')
    slot.id = 'slot'
    document.body.appendChild(slot)
    const { widget, root } = widgetHarness({ mode: 'inline', target: '#slot' })

    widget.close()

    expect((root.querySelector('.panel') as HTMLElement).dataset.open).toBe('true')
  })

  it('honours open:true so a host can start expanded', () => {
    const { widget } = widgetHarness({ open: true })
    expect(widget.isOpen()).toBe(true)
  })

  it('lets the frame ask to be closed', async () => {
    const { widget, fromFrame } = widgetHarness({ open: true })
    await fromFrame({ source: SOURCE, type: 'close' })
    expect(widget.isOpen()).toBe(false)
  })

  it('removes the launcher when the frame says there is nothing here for this visitor', async () => {
    const { widget, root, fromFrame } = widgetHarness({ open: true })
    await fromFrame({ source: SOURCE, type: 'unavailable' })
    expect(widget.isOpen()).toBe(false)
    expect(widget.isDismissed()).toBe(true)
    expect(root.querySelector('.launcher')).toBeNull()
  })

  it('lets the frame set its own height in overlay mode', async () => {
    const { root, fromFrame } = widgetHarness()
    await fromFrame({ source: SOURCE, type: 'resize', height: 300 })
    expect((root.querySelector('.panel') as HTMLElement).style.height).toBe('300px')
  })
})

describe('the credential path', () => {
  it('mints on ready and posts the token to the canopy origin, never *', async () => {
    const { fromFrame, sent } = widgetHarness()

    await fromFrame({ source: SOURCE, type: 'ready' })

    const [init_] = sent('init')
    expect(init_.message.token).toBe('delegated-tok')
    expect(init_.targetOrigin).toBe(CANOPY)
    expect(init_.targetOrigin).not.toBe('*')
  })

  it('lets the frame copy to the clipboard and ask for a passkey, and nothing more', () => {
    const { iframe } = widgetHarness()
    const allow = (iframe.getAttribute('allow') ?? '').split(';').map((p) => p.trim())
    expect(allow).toEqual(['publickey-credentials-get', 'clipboard-write'])
  })

  it('passes a shorter permission window from the host page URL to the frame', () => {
    window.history.replaceState({}, '', '/?canopy_debug_window=30')
    const { iframe } = widgetHarness()
    window.history.replaceState({}, '', '/')
    expect(iframe.src).toContain('&debug_window=30')
  })

  it.each(['soon', '-5', '30;x'])('ignores a debug window of %s', (value) => {
    window.history.replaceState({}, '', `/?canopy_debug_window=${encodeURIComponent(value)}`)
    const { iframe } = widgetHarness()
    window.history.replaceState({}, '', '/')
    expect(iframe.src).not.toContain('debug_window')
  })

  it('asks for no debug window by default', () => {
    expect(widgetHarness().iframe.src).not.toContain('debug_window')
  })

  it('never puts the token in the iframe URL', async () => {
    const { iframe, fromFrame } = widgetHarness()
    await fromFrame({ source: SOURCE, type: 'ready' })
    // A fragment or query token would land in document.location, history and
    // potentially a referrer.
    expect(iframe.src).not.toContain('delegated-tok')
    expect(iframe.src).not.toContain('token')
  })

  it('calls the host token endpoint same-origin with credentials', async () => {
    const { fromFrame } = widgetHarness()
    await fromFrame({ source: SOURCE, type: 'ready' })

    const [url, init_] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(url).toBe(TOKEN_URL)
    expect(init_.credentials).toBe('same-origin')
    expect(init_.method).toBe('POST')
  })

  it('never asks the host for an act-as-you permission on load, only when the frame says so', async () => {
    const { fromFrame, sent } = widgetHarness()
    await fromFrame({ source: SOURCE, type: 'ready' })
    await fromFrame({ source: SOURCE, type: 'token-request', id: 'r1' })
    await fromFrame({ source: SOURCE, type: 'token-request', id: 'r2', grant: true })
    await fromFrame({ source: SOURCE, type: 'token-request', id: 'r3', grant: 'yes' })

    const bodies = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[1].body)
    expect(bodies).toEqual(['{}', '{}', '{"grant":true}', '{}'])
    // The expiry rides with the token, so the frame's client can cache it.
    expect(sent('init')[0].message).toHaveProperty('expires_at')
    expect(sent('token')[0].message).toHaveProperty('expires_at')
  })

  it('reads a tokenUrl function on every mint, so a later mint names the page now on screen', async () => {
    // canopy's own single-page app: the widget outlives navigation, and the
    // token endpoint needs the page the visitor is on AT MINT TIME.
    let page = 'projects'
    const { fromFrame } = widgetHarness({ tokenUrl: () => `${TOKEN_URL}?page=${page}` })
    await fromFrame({ source: SOURCE, type: 'ready' })
    page = 'agent.tasks'
    await fromFrame({ source: SOURCE, type: 'token-request', id: 'r2' })

    const urls = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0])
    expect(urls).toEqual([`${TOKEN_URL}?page=projects`, `${TOKEN_URL}?page=agent.tasks`])
  })

  it('sends the CSRF header when the host uses a csrftoken cookie', async () => {
    document.cookie = 'csrftoken=abc123'
    const { fromFrame } = widgetHarness()
    await fromFrame({ source: SOURCE, type: 'ready' })

    const [, init_] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(init_.headers['X-CSRFToken']).toBe('abc123')
  })

  it('reads the cookie name the host named, not Django\'s default', async () => {
    // THE bug this option exists for. A Django app served under a path prefix
    // on a shared host has to rename its CSRF cookie or it collides with its
    // siblings — canopy on labs uses `csrftoken_canopy`. Hard-coding
    // `csrftoken` meant no header at all, a 403 from the mint, and a widget
    // that silently never started on the one deployment it was built for.
    document.cookie = 'csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
    document.cookie = 'csrftoken_canopy=scoped-value'

    const { fromFrame } = widgetHarness({ csrfCookieName: 'csrftoken_canopy' })
    await fromFrame({ source: SOURCE, type: 'ready' })

    const [, init_] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(init_.headers['X-CSRFToken']).toBe('scoped-value')
  })

  it('does not pick up a cookie whose name merely resembles the configured one', async () => {
    // `csrftoken` is a prefix of `csrftoken_canopy`, so an unanchored match
    // would send the wrong app's token — a 403 that looks like a login problem.
    document.cookie = 'csrftoken_canopy=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
    document.cookie = 'csrftoken=root-tenant'

    const { fromFrame } = widgetHarness({ csrfCookieName: 'csrftoken_canopy' })
    await fromFrame({ source: SOURCE, type: 'ready' })

    const [, init_] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(init_.headers['X-CSRFToken']).toBeUndefined()
  })

  it('sends a csrfToken the host supplied, when no cookie is readable at all', async () => {
    // THE bug this option exists for. A Django host with
    // `CSRF_COOKIE_HTTPONLY = True` (connect-labs) hides the cookie from
    // `document.cookie` at every name, so `csrfCookieName` cannot rescue it:
    // the header is absent, the mint 403s, and the widget silently never
    // starts. Such a host renders the token into the page instead.
    document.cookie = 'csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT'

    const { fromFrame } = widgetHarness({ csrfToken: 'from-the-dom' })
    await fromFrame({ source: SOURCE, type: 'ready' })

    const [, init_] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(init_.headers['X-CSRFToken']).toBe('from-the-dom')
  })

  it('re-reads a csrfToken provider on every mint, so a rotated token is not stale', async () => {
    // Django rotates the CSRF token on login. A value captured once at `init`
    // would then be wrong for every mint after that, which fails as a 403 long
    // after the call that caused it.
    let current = 'first'
    const { fromFrame } = widgetHarness({ csrfToken: () => current })
    await fromFrame({ source: SOURCE, type: 'ready' })
    current = 'rotated'
    await fromFrame({ source: SOURCE, type: 'token-request', id: 'r1' })

    const calls = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls
    expect(calls[0][1].headers['X-CSRFToken']).toBe('first')
    expect(calls[1][1].headers['X-CSRFToken']).toBe('rotated')
  })

  it('prefers the supplied csrfToken over a cookie that happens to be readable', async () => {
    // A host passing this has said the cookie is not the source of truth.
    // Falling back to one that merely exists could send another tenant's token.
    document.cookie = 'csrftoken=stale-cookie'

    const { fromFrame } = widgetHarness({ csrfToken: () => 'from-the-dom' })
    await fromFrame({ source: SOURCE, type: 'ready' })

    const [, init_] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(init_.headers['X-CSRFToken']).toBe('from-the-dom')
  })

  it('mints without the header when the provider throws or finds nothing', async () => {
    // A selector that matches nothing throws on `.value`. The mint's own 403
    // names the problem; an exception escaping here would instead look like the
    // widget failing to start for no stated reason.
    document.cookie = 'csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT'

    const { fromFrame } = widgetHarness({
      csrfToken: () => {
        throw new Error('no such element')
      },
    })
    await fromFrame({ source: SOURCE, type: 'ready' })

    const [, init_] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(init_.headers['X-CSRFToken']).toBeUndefined()
    expect(init_.method).toBe('POST')
  })

  it('answers a refresh request with a fresh mint, keyed by id', async () => {
    const { fromFrame, sent } = widgetHarness()
    await fromFrame({ source: SOURCE, type: 'token-request', id: 'r1' })

    const [token] = sent('token')
    expect(token.message).toMatchObject({ id: 'r1', token: 'delegated-tok' })
  })

  it('tells the frame when minting fails, instead of leaving it on a spinner', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 403, json: async () => ({}) })))
    const { fromFrame, sent } = widgetHarness()

    await fromFrame({ source: SOURCE, type: 'ready' })

    expect(sent('init')).toHaveLength(0)
    expect(sent('token-error')[0].message.message).toContain('403')
  })

  it('treats a 200 with no token as a failure', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) })))
    const { fromFrame, sent } = widgetHarness()

    await fromFrame({ source: SOURCE, type: 'ready' })

    expect(sent('token-error')[0].message.message).toContain('no token')
  })
})

describe('origin discipline', () => {
  it('ignores a message from another origin', async () => {
    const { fromFrame, sent } = widgetHarness()
    await fromFrame({ source: SOURCE, type: 'ready' }, { origin: 'https://evil.example' })
    expect(sent('init')).toHaveLength(0)
    expect(fetch).not.toHaveBeenCalled()
  })

  it('ignores a right-origin message from a different frame', async () => {
    // Origin alone is not enough: any other frame served from canopy's origin
    // would otherwise be able to ask the host to mint a token.
    const { fromFrame, sent } = widgetHarness()
    await fromFrame({ source: SOURCE, type: 'ready' }, { source: {} })
    expect(sent('init')).toHaveLength(0)
  })

  it('ignores messages that are not ours', async () => {
    const { fromFrame, sent } = widgetHarness()
    await fromFrame({ type: 'ready' })
    await fromFrame({ source: 'some-other-widget', type: 'ready' })
    await fromFrame('a string')
    await fromFrame(null)
    expect(sent('init')).toHaveLength(0)
  })

  it('refuses to interpret a HOST-bound message arriving at the host', async () => {
    // The host is the only party that issues `init`/`token`; one arriving here
    // is impersonation, not input.
    const { fromFrame, posted } = widgetHarness()
    await fromFrame({ source: SOURCE, type: 'init', token: 'attacker', actions: [] })
    await fromFrame({ source: SOURCE, type: 'token', id: 'x', token: 'attacker' })
    expect(posted).toHaveLength(0)
  })

  it('derives the canopy origin from a relative base as the page origin', () => {
    // The same-origin deployment: /canopy really is this page's origin.
    expect(originOf('/canopy', 'https://labs.connect.dimagi.com')).toBe(
      'https://labs.connect.dimagi.com',
    )
    expect(originOf('https://canopy.example.com/canopy', 'https://labs.x')).toBe(CANOPY)
  })
})

describe('context', () => {
  it('reads the page state at request time, not at registration', async () => {
    const { widget, fromFrame, sent } = widgetHarness()
    let step = 1
    widget.provideContext(() => ({ step }))

    await fromFrame({ source: SOURCE, type: 'context-request', id: 'c1' })
    expect(sent('context')[0].message.context).toEqual({ step: 1 })

    step = 2
    await fromFrame({ source: SOURCE, type: 'context-request', id: 'c2' })
    expect(sent('context')[1].message.context).toEqual({ step: 2 })
  })

  it('answers {} when the host lends no context', async () => {
    const { fromFrame, sent } = widgetHarness()
    await fromFrame({ source: SOURCE, type: 'context-request', id: 'c1' })
    expect(sent('context')[0].message.context).toEqual({})
  })

  it('awaits an async provider', async () => {
    const { widget, fromFrame, sent } = widgetHarness()
    widget.provideContext(async () => ({ loaded: true }))
    await fromFrame({ source: SOURCE, type: 'context-request', id: 'c1' })
    expect(sent('context')[0].message.context).toEqual({ loaded: true })
  })

  it('a throwing provider does not take the conversation down', async () => {
    const { widget, fromFrame, sent } = widgetHarness()
    widget.provideContext(() => {
      throw new Error('host bug')
    })
    await fromFrame({ source: SOURCE, type: 'context-request', id: 'c1' })
    expect(sent('context')[0].message.context).toEqual({})
  })
})

describe('actions', () => {
  it('runs a registered action and returns its result', async () => {
    const { widget, fromFrame, sent } = widgetHarness()
    const onUpdateState = vi.fn().mockResolvedValue({ saved: true })
    widget.registerAction('updateState', onUpdateState)

    await fromFrame({
      source: SOURCE,
      type: 'action-request',
      id: 'a1',
      name: 'updateState',
      args: { status: 'reviewed' },
    })

    expect(onUpdateState).toHaveBeenCalledWith({ status: 'reviewed' })
    expect(sent('action-result')[0].message).toMatchObject({ id: 'a1', result: { saved: true } })
  })

  it('errors on an unknown action instead of silently doing nothing', async () => {
    const { fromFrame, sent } = widgetHarness()
    await fromFrame({ source: SOURCE, type: 'action-request', id: 'a1', name: 'nope' })
    expect(sent('action-error')[0].message.message).toContain('nope')
  })

  it('reports a host refusal as an error the agent can read', async () => {
    const { widget, fromFrame, sent } = widgetHarness()
    widget.registerAction('act', () => {
      throw new Error('not allowed on a completed run')
    })
    await fromFrame({ source: SOURCE, type: 'action-request', id: 'a1', name: 'act' })
    expect(sent('action-error')[0].message.message).toBe('not allowed on a completed run')
  })

  it('declares the action list on init, with schemas', async () => {
    const { widget, fromFrame, sent } = widgetHarness()
    widget.registerAction('b', vi.fn())
    widget.registerAction('a', vi.fn(), {
      description: 'Dismiss things',
      parameters: { type: 'object', properties: { ids: { type: 'array' } }, required: ['ids'] },
    })

    await fromFrame({ source: SOURCE, type: 'ready' })

    const declared = sent('init')[0].message.actions as Array<Record<string, unknown>>
    // Sorted, so a host registering in a different order across renders does
    // not hand the agent a different-looking tool list each time.
    expect(declared.map((a) => a.name)).toEqual(['a', 'b'])
    // The schema is the whole reason this is a spec rather than a name: the
    // agent cannot call `a` without being told it takes `ids`.
    expect(declared[0].parameters).toMatchObject({ required: ['ids'] })
    expect(declared[0].description).toBe('Dismiss things')
  })

  it('tells an open frame when the action set changes', async () => {
    const { widget, sent } = widgetHarness()
    widget.registerAction('a', vi.fn())
    expect((sent('actions')[0].message.actions as Array<{ name: string }>).map((a) => a.name))
      .toEqual(['a'])

    widget.unregisterAction('a')
    expect(sent('actions')[1].message.actions).toEqual([])
  })

  it('re-registering replaces the handler rather than stacking', async () => {
    const { widget, fromFrame } = widgetHarness()
    const stale = vi.fn()
    const fresh = vi.fn()
    widget.registerAction('act', stale)
    widget.registerAction('act', fresh)

    await fromFrame({ source: SOURCE, type: 'action-request', id: 'a1', name: 'act' })

    expect(stale).not.toHaveBeenCalled()
    expect(fresh).toHaveBeenCalledOnce()
  })
})

describe('destroy', () => {
  it('removes the chrome and stops listening', async () => {
    const { widget, fromFrame, posted } = widgetHarness()
    widget.destroy()

    expect(document.querySelector('[data-canopy-widget]')).toBeNull()
    await fromFrame({ source: SOURCE, type: 'ready' })
    expect(posted).toHaveLength(0)
  })

  it('is idempotent', () => {
    const { widget } = widgetHarness()
    widget.destroy()
    expect(() => widget.destroy()).not.toThrow()
  })

  it('two widgets on one page do not interfere', () => {
    const a = widgetHarness()
    const b = widgetHarness()
    expect(document.querySelectorAll('[data-canopy-widget]')).toHaveLength(2)
    a.widget.destroy()
    expect(document.querySelectorAll('[data-canopy-widget]')).toHaveLength(1)
    b.widget.destroy()
  })
})

describe('the launcher belongs to the host page', () => {
  it('uses the label the host gave it', () => {
    const { root } = widgetHarness({ launcherLabel: 'Canopy AI' })
    expect((root.querySelector('.launcher') as HTMLElement).textContent).toBe('Canopy AI')
  })

  it('falls back to a sensible default when the host names nothing', () => {
    const { root } = widgetHarness()
    expect((root.querySelector('.launcher') as HTMLElement).textContent).toBe('Ask Canopy')
  })

  it('can be dismissed, which hides it for the rest of the page load', () => {
    // It is fixed to the corner of somebody else's page — on a phone it lands
    // on whatever is already there. Somebody who wants the page rather than the
    // agent needs a way to say so that is not "reload and hope".
    const { root, widget } = widgetHarness({ launcherLabel: 'Canopy AI' })
    const dismiss = root.querySelector('.dismiss') as HTMLButtonElement
    expect(dismiss).not.toBeNull()

    dismiss.click()

    // GONE from the tree, not merely flagged. The first version set
    // `dock.hidden = true`, and this test asserted that attribute — which
    // passed while the bubble stayed visible in a real browser, because
    // `.dock { display: flex }` outranks the UA stylesheet's
    // `[hidden] { display: none }`. jsdom does not reproduce that cascade, so
    // the only assertion that can catch it is the node's absence.
    expect(root.querySelector('.dock')).toBeNull()
    expect(root.querySelector('.launcher')).toBeNull()
    expect(widget.isDismissed()).toBe(true)
  })

  it('closes an open panel when dismissed, leaving nothing stranded', () => {
    // Otherwise the panel stays on screen with its launcher gone, and the only
    // way out is the frame's own close button.
    const { root, widget } = widgetHarness({ open: true })
    expect(widget.isOpen()).toBe(true)

    ;(root.querySelector('.dismiss') as HTMLButtonElement).click()

    expect(widget.isOpen()).toBe(false)
  })

  it('the X is a sibling of the bubble, not a button inside a button', () => {
    // It overlaps the bubble visually, which is what made me write a
    // stopPropagation guard and a test that "proved" it — both vacuous, since
    // a sibling click never passes through. Nesting it would make the guard
    // necessary AND make the markup invalid, so assert the structure instead.
    const { root } = widgetHarness()
    const launcher = root.querySelector('.launcher') as HTMLElement
    const dismiss = root.querySelector('.dismiss') as HTMLElement
    expect(launcher.contains(dismiss)).toBe(false)
    expect(dismiss.parentElement).toBe(launcher.parentElement)
  })

  it('a host that laid out around the launcher can turn the X off', () => {
    const { root } = widgetHarness({ dismissible: false })
    expect(root.querySelector('.launcher')).not.toBeNull()
    expect(root.querySelector('.dismiss')).toBeNull()
  })

  it('inline mode has neither, since it has no launcher at all', () => {
    const slot = document.createElement('div')
    slot.id = 'slot2'
    document.body.appendChild(slot)
    const { root } = widgetHarness({ mode: 'inline', target: '#slot2' })
    expect(root.querySelector('.dock')).toBeNull()
    expect(root.querySelector('.dismiss')).toBeNull()
  })
})

describe('dragging the bubble out of the way', () => {
  /** jsdom has no PointerEvent, so synthesise one that carries what the
   *  handlers read. Testing the wiring, not the browser's gesture engine —
   *  the geometry it drives is covered in dragging.test.ts. */
  function pointer(type: string, x: number, y: number) {
    const e = new Event(type, { bubbles: true, cancelable: true }) as Event & {
      clientX: number
      clientY: number
      pointerId: number
    }
    e.clientX = x
    e.clientY = y
    e.pointerId = 1
    return e
  }

  function drag(launcher: Element, from: [number, number], to: [number, number]) {
    launcher.dispatchEvent(pointer('pointerdown', ...from))
    launcher.dispatchEvent(pointer('pointermove', ...to))
    launcher.dispatchEvent(pointer('pointerup', ...to))
  }

  it('moves the dock, and marks it as placed by coordinates', () => {
    const { root } = widgetHarness()
    const launcher = root.querySelector('.launcher')!
    const dock = root.querySelector('.dock') as HTMLElement

    drag(launcher, [500, 500], [200, 300])

    // `data-moved` is what turns off the corner insets in CSS; without it the
    // left/top coordinates fight `right`/`bottom` and nothing appears to move.
    expect(dock.dataset.moved).toBe('true')
    expect(dock.style.left).not.toBe('')
    expect(dock.style.top).not.toBe('')
  })

  it('does not open the panel when the drag ends over the bubble', () => {
    // A drag that finishes where it started still fires a click. Without the
    // capture-phase guard the panel opens every time you put the bubble down.
    const { root, widget } = widgetHarness()
    const launcher = root.querySelector('.launcher') as HTMLElement

    drag(launcher, [500, 500], [200, 300])
    launcher.click()

    expect(widget.isOpen()).toBe(false)
  })

  it('still opens on a plain tap', () => {
    // The guard must not swallow ordinary clicks — that would leave the widget
    // unopenable, which is worse than the bug it prevents.
    const { root, widget } = widgetHarness()
    const launcher = root.querySelector('.launcher') as HTMLElement

    launcher.click()

    expect(widget.isOpen()).toBe(true)
  })

  it('a wobble is a tap, not a drag', () => {
    const { root, widget } = widgetHarness()
    const launcher = root.querySelector('.launcher') as HTMLElement
    const dock = root.querySelector('.dock') as HTMLElement

    drag(launcher, [500, 500], [502, 501])
    launcher.click()

    expect(dock.dataset.moved).toBeUndefined()
    expect(widget.isOpen()).toBe(true)
  })

  it('remembers the position for next time', () => {
    const storage = memoryStorage()
    const { root } = widgetHarness({ app: 'canopy-web', storage })

    drag(root.querySelector('.launcher')!, [500, 500], [200, 300])

    const saved = storage.data['canopy.widget.position.canopy-web']
    expect(saved).toBeTruthy()
    expect(JSON.parse(saved)).toHaveProperty('x')
  })

  it('restores a saved position on the next load', () => {
    const storage = memoryStorage({
      'canopy.widget.position.canopy-web': JSON.stringify({ x: 120, y: 240 }),
    })

    const { root } = widgetHarness({ app: 'canopy-web', storage })
    const dock = root.querySelector('.dock') as HTMLElement

    expect(dock.dataset.moved).toBe('true')
    expect(dock.style.left).toBe('120px')
  })

  it('a window resize leaves a docked rail on the right and the bubble where it was put', () => {
    // jsdom measures every element 0×0 at the origin — exactly what a hidden
    // dock (covered by the open rail) measures in a real browser, which is how
    // a resize used to throw both into the top-left corner.
    const storage = memoryStorage({
      'canopy.widget.position.connect-labs': JSON.stringify({ x: 120, y: 240 }),
    })
    const { root } = widgetHarness({ mode: 'docked', storage, open: true })
    const panel = root.querySelector('.panel') as HTMLElement
    const dock = root.querySelector('.dock') as HTMLElement

    window.dispatchEvent(new Event('resize'))

    expect(panel.style.left).toBe('')
    expect(panel.style.top).toBe('')
    expect(dock.style.left).toBe('120px')
    expect(dock.style.top).toBe('240px')
  })

  it('a window resize does not throw a floating panel into the corner either', () => {
    const storage = memoryStorage({
      'canopy.widget.position.connect-labs': JSON.stringify({ x: 120, y: 240 }),
    })
    for (const mode of ['overlay', 'docked'] as const) {
      const { root } = widgetHarness({ mode, storage, open: true })
      const panel = root.querySelector('.panel') as HTMLElement
      window.dispatchEvent(new Event('resize'))
      expect(panel.style.left).not.toBe('8px')
      expect(panel.style.top).not.toBe('8px')
      root.host.remove()
    }
  })

  it('works with no storage at all — dragging is not a storage feature', () => {
    const { root } = widgetHarness({ storage: null })
    const dock = root.querySelector('.dock') as HTMLElement

    drag(root.querySelector('.launcher')!, [500, 500], [200, 300])

    expect(dock.dataset.moved).toBe('true')
  })
})

describe('resizing the panel by its corner', () => {
  function pointer(type: string, x: number, y: number) {
    const e = new Event(type, { bubbles: true, cancelable: true }) as Event & {
      clientX: number
      clientY: number
      pointerId: number
    }
    e.clientX = x
    e.clientY = y
    e.pointerId = 1
    return e
  }

  function resize(grip: Element, from: [number, number], to: [number, number]) {
    grip.dispatchEvent(pointer('pointerdown', ...from))
    grip.dispatchEvent(pointer('pointermove', ...to))
    grip.dispatchEvent(pointer('pointerup', ...to))
  }

  it('grows the panel as its top-left corner is dragged out, and remembers it', () => {
    const storage = memoryStorage()
    const { root } = widgetHarness({ app: 'canopy-web', storage, width: 400, open: true })
    const panel = root.querySelector('.panel') as HTMLElement

    resize(root.querySelector('.resize')!, [100, 100], [0, 50])

    // jsdom has no layout, so the start is the configured width and the
    // default height the panel falls back to.
    expect(panel.style.width).toBe('500px')
    expect(panel.style.height).toBe('550px')
    expect(panel.dataset.resizing).toBe('false')
    expect(JSON.parse(storage.data['canopy.widget.size.canopy-web'])).toEqual({ width: 500, height: 550 })
  })

  it('restores a saved size on the next load', () => {
    const storage = memoryStorage({
      'canopy.widget.size.canopy-web': JSON.stringify({ width: 520, height: 480 }),
    })
    const { root } = widgetHarness({ app: 'canopy-web', storage })
    const panel = root.querySelector('.panel') as HTMLElement
    expect(panel.style.width).toBe('520px')
    expect(panel.style.height).toBe('480px')
  })

  it('a visitor-chosen size beats the height the frame asks for', async () => {
    const { root, fromFrame } = widgetHarness({ open: true })
    const panel = root.querySelector('.panel') as HTMLElement

    resize(root.querySelector('.resize')!, [100, 100], [50, 50])
    const chosen = panel.style.height
    await fromFrame({ source: SOURCE, type: 'resize', height: 300 })

    expect(panel.style.height).toBe(chosen)
  })

  it('a docked rail resizes by its corner too, sitting on the window\'s bottom', () => {
    const storage = memoryStorage()
    const onResize = vi.fn()
    const { root } = widgetHarness({ mode: 'docked', app: 'canopy-web', storage, width: 400, open: true, onResize })
    const panel = root.querySelector('.panel') as HTMLElement

    resize(root.querySelector('.resize')!, [600, 0], [450, 100])

    expect(panel.style.width).toBe('550px')
    // Starts full height (the window's), so dragging the corner down 100px
    // drops its top by that much; its bottom stays on the window's edge.
    expect(panel.style.height).toBe(`${window.innerHeight - 100}px`)
    expect(panel.style.top).toBe('auto')
    expect(panel.dataset.short).toBe('true')
    expect(onResize).toHaveBeenLastCalledWith({ width: 550, height: window.innerHeight - 100 })
    expect(JSON.parse(storage.data['canopy.widget.size.canopy-web'])).toEqual({
      width: 550,
      height: window.innerHeight - 100,
    })
  })

  it('a docked rail dragged back to the top is a full-height rail again', () => {
    const { root } = widgetHarness({ mode: 'docked', width: 400, open: true })
    const panel = root.querySelector('.panel') as HTMLElement
    const grip = root.querySelector('.resize')!

    resize(grip, [600, 0], [600, 200])
    expect(panel.dataset.short).toBe('true')
    resize(grip, [600, 200], [600, -50])

    // No inline height: the CSS rail (100vh), so it follows the window.
    expect(panel.style.height).toBe('')
    expect(panel.style.top).toBe('')
    expect(panel.dataset.short).toBe('false')
  })

  it('restores a docked size and reports it, so the host can lay out for it', () => {
    const storage = memoryStorage({
      'canopy.widget.size.canopy-web': JSON.stringify({ width: 520, height: 480 }),
    })
    const onResize = vi.fn()
    const { root } = widgetHarness({ mode: 'docked', app: 'canopy-web', storage, onResize })
    const panel = root.querySelector('.panel') as HTMLElement
    expect(panel.style.width).toBe('520px')
    expect(panel.style.height).toBe('480px')
    expect(onResize).toHaveBeenCalledWith({ width: 520, height: 480 })
  })

  it('an inline panel has no grip — the host sizes it', () => {
    document.body.innerHTML = '<div id="slot"></div>'
    expect(widgetHarness({ mode: 'inline', target: '#slot' }).root.querySelector('.resize')).toBeNull()
  })
})

describe('snapping a docked rail into the page', () => {
  afterEach(() => {
    document.documentElement.style.paddingRight = ''
  })

  it('reserves the rail\'s width on the page while open, and gives it back on close', async () => {
    const storage = memoryStorage()
    const { widget, fromFrame, sent } = widgetHarness({ mode: 'docked', width: 560, open: true, storage })
    await fromFrame({ source: SOURCE, type: 'ready' })
    expect(sent('init')[0].message).toMatchObject({ snappable: true, snapped: false })
    document.documentElement.style.paddingRight = '3px' // the page's own value

    await fromFrame({ source: SOURCE, type: 'snap', snapped: true })

    expect(document.documentElement.style.paddingRight).toBe('560px')
    expect(sent('display').at(-1)!.message).toMatchObject({ snapped: true })
    expect(storage.data['canopy.widget.snap.connect-labs']).toBe('snapped')

    widget.close()
    expect(document.documentElement.style.paddingRight).toBe('3px')
    widget.open()
    expect(document.documentElement.style.paddingRight).toBe('560px')
  })

  it('follows the rail\'s width as its divider is dragged, keeping the floating height', async () => {
    const storage = memoryStorage({
      'canopy.widget.size.connect-labs': JSON.stringify({ width: 400, height: 480 }),
    })
    const onResize = vi.fn()
    const { root, fromFrame } = widgetHarness({ mode: 'docked', width: 400, open: true, storage, onResize })
    await fromFrame({ source: SOURCE, type: 'snap', snapped: true })
    const grip = root.querySelector('.divider')!
    const at = (type: string, x: number) => {
      const e = new Event(type) as Event & { clientX: number; clientY: number; pointerId: number }
      e.clientX = x
      e.clientY = 300
      e.pointerId = 1
      grip.dispatchEvent(e)
    }
    at('pointerdown', 600)
    at('pointermove', 450)
    at('pointerup', 450)
    expect(document.documentElement.style.paddingRight).toBe('550px')
    // Snapped it is full height, so that is what the host hears…
    expect(onResize).toHaveBeenLastCalledWith({ width: 550, height: window.innerHeight })
    // …while the shorter height chosen floating survives for when it floats.
    expect(JSON.parse(storage.data['canopy.widget.size.connect-labs'])).toEqual({ width: 550, height: 480 })
  })

  it('a snapped rail is sized by its divider only — the corner grip steps aside', async () => {
    const { root, fromFrame } = widgetHarness({ mode: 'docked', width: 400, open: true })
    const panel = root.querySelector('.panel') as HTMLElement
    await fromFrame({ source: SOURCE, type: 'snap', snapped: true })
    expect(panel.dataset.snapped).toBe('true')
    expect(root.querySelector('.divider')!.getAttribute('role')).toBe('separator')
    // The rule that hides the grip and pins the height is CSS; jsdom has no
    // cascade, so assert the rule exists rather than a computed style.
    const css = root.querySelector('style')!.textContent!
    expect(css).toMatch(/\.panel\[data-snapped="true"\] \.resize \{ display: none; \}/)
    expect(css).toMatch(/\.panel\[data-snapped="true"\] \{[^}]*height: auto !important/)
  })

  it('an overlay panel has no divider', () => {
    expect(widgetHarness({ open: true }).root.querySelector('.divider')).toBeNull()
  })

  it('lets go while expanded, and takes the room back on collapse', async () => {
    const { widget, fromFrame } = widgetHarness({ mode: 'docked', width: 500, open: true })
    await fromFrame({ source: SOURCE, type: 'snap', snapped: true })
    widget.expand()
    expect(document.documentElement.style.paddingRight).toBe('')
    widget.collapse()
    expect(document.documentElement.style.paddingRight).toBe('500px')
  })

  it('pads the element the host names, and restores it on destroy', () => {
    document.body.innerHTML = '<div id="scroller"></div>'
    const scroller = document.getElementById('scroller')!
    const { widget } = widgetHarness({ mode: 'docked', width: 480, open: true, snapTarget: '#scroller' })
    widget.snap()
    expect(scroller.style.paddingRight).toBe('480px')
    expect(document.documentElement.style.paddingRight).toBe('')
    widget.destroy()
    expect(scroller.style.paddingRight).toBe('')
  })

  it('restores a remembered snap', () => {
    const storage = memoryStorage({ 'canopy.widget.snap.connect-labs': 'snapped' })
    const { widget } = widgetHarness({ mode: 'docked', width: 520, open: true, storage })
    expect(widget.isSnapped()).toBe(true)
    expect(document.documentElement.style.paddingRight).toBe('520px')
  })

  it('is docked only, and off when the host says so', async () => {
    const overlay = widgetHarness({ open: true })
    overlay.widget.snap()
    expect(overlay.widget.isSnapped()).toBe(false)
    await overlay.fromFrame({ source: SOURCE, type: 'ready' })
    expect(overlay.sent('init')[0].message.snappable).toBe(false)

    const off = widgetHarness({ mode: 'docked', snappable: false, open: true })
    off.widget.snap()
    expect(off.widget.isSnapped()).toBe(false)
    expect(document.documentElement.style.paddingRight).toBe('')
  })
})

describe('the page state channel', () => {
  it('pushes the view to an open frame, on its own message', async () => {
    const { widget, fromFrame, sent } = widgetHarness()
    await fromFrame({ source: SOURCE, type: 'ready' })

    widget.setPageState({ visible_ids: [4471, 4472], backing_tool: 'list_items' })

    const [push] = sent('state')
    expect(push.message.state).toEqual({
      visible_ids: [4471, 4472],
      backing_tool: 'list_items',
    })
    // The canopy origin, never '*' — the view describes the user's screen.
    expect(push.targetOrigin).toBe(CANOPY)
  })

  it('replays the last view on init, so a panel opened later is not blind', async () => {
    // The host declares its state when the PAGE renders; the frame handshakes
    // when the user opens the panel, which can be much later. Without the
    // replay the agent would know the view only if the user happened to filter
    // the page after opening the chat.
    const { widget, fromFrame, sent } = widgetHarness()

    widget.setPageState({ visible_ids: [7] })
    await fromFrame({ source: SOURCE, type: 'ready' })

    const pushes = sent('state')
    expect(pushes.at(-1)?.message.state).toEqual({ visible_ids: [7] })
  })

  it('sends the view AFTER init, not inside it', async () => {
    // `init` is the one message carrying the token and the one whose shape the
    // frame validates strictly. The view is a separate fact and rides its own
    // frame rather than growing a field on the credential message.
    const { widget, fromFrame, posted } = widgetHarness()
    widget.setPageState({ visible_ids: [7] })

    await fromFrame({ source: SOURCE, type: 'ready' })

    const kinds = posted.map((p) => p.message.type)
    expect(kinds.indexOf('init')).toBeLessThan(kinds.indexOf('state'))
    expect(posted.find((p) => p.message.type === 'init')?.message).not.toHaveProperty('state')
  })

  it('says nothing at all when the host never declares a view', async () => {
    const { fromFrame, sent } = widgetHarness()

    await fromFrame({ source: SOURCE, type: 'ready' })

    // A host that does not use the channel must not have an empty screen
    // announced on its behalf.
    expect(sent('state')).toHaveLength(0)
  })
})


/**
 * Theming. The launcher is host DOM in our shadow root and the panel is
 * canopy's own document, so a theme has to reach both — by custom properties
 * and `::part()` on one side and by the handshake on the other.
 */
describe('theming', () => {
  it('changes nothing for a host that sets no theme', () => {
    const { iframe, host } = widgetHarness()
    // No `&theme=` means the server renders the historical dark default.
    expect(iframe.src).not.toContain('theme=')
    expect(host.style.getPropertyValue('--canopy-accent')).toBe('')
    expect(host.dataset.theme).toBe('dark')
  })

  it('puts the mode in the iframe URL, so the shell renders it from the first byte', () => {
    const { iframe } = widgetHarness({ theme: { mode: 'light' } })
    expect(iframe.src).toContain('&theme=light')
  })

  it('dresses the launcher through the documented variables', () => {
    const { host, root } = widgetHarness({ theme: { mode: 'light', accent: '#2563eb', radius: 8 } })
    expect(host.style.getPropertyValue('--canopy-accent')).toBe('#2563eb')
    expect(host.style.getPropertyValue('--canopy-accent-foreground')).toBe('#ffffff')
    expect(host.style.getPropertyValue('--canopy-radius')).toBe('8px')
    expect(host.dataset.theme).toBe('light')
    expect((root.querySelector('.panel') as HTMLElement).dataset.theme).toBe('light')
  })

  it('names the parts a host may style explicitly', () => {
    const { root } = widgetHarness()
    expect(root.querySelector('[part="launcher"]')).not.toBeNull()
    expect(root.querySelector('[part="dismiss"]')).not.toBeNull()
    expect(root.querySelector('[part="panel"]')).not.toBeNull()
  })

  it('reads every colour from a variable whose fallback is today\'s value', () => {
    // The promise that a host setting nothing sees no change, pinned at the
    // source: if a hard-coded colour creeps back into the launcher, a host's
    // `--canopy-accent` silently stops working.
    const { root } = widgetHarness()
    const css = root.querySelector('style')!.textContent!
    expect(css).toContain('var(--canopy-accent, #c2410c)')
    expect(css).not.toMatch(/\.launcher\s*\{[^}]*background:\s*#/)
    // The general radius reaches the launcher; the launcher-only one wins.
    expect(css).toContain('var(--canopy-launcher-radius, var(--canopy-radius, 24px))')
  })

  it('refuses a hostile accent and says so', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const { host } = widgetHarness({ theme: { accent: 'red; background: url(https://evil.example)' } })
    expect(host.style.getPropertyValue('--canopy-accent')).toBe('')
    expect(warn).toHaveBeenCalled()
  })

  it('hands the validated theme to the frame in init', async () => {
    const { fromFrame, sent } = widgetHarness({ theme: { mode: 'light', accent: '#2563eb' } })
    await fromFrame({ source: SOURCE, type: 'ready' })
    const [init] = sent('init')
    expect(init.message.theme).toEqual({
      mode: 'light', accent: '#2563eb', accentForeground: '#ffffff',
    })
  })

  it('re-themes live: the launcher at once, the frame once it is listening', async () => {
    const { widget, host, fromFrame, sent } = widgetHarness({ theme: { mode: 'dark' } })
    widget.setTheme({ mode: 'light', accent: '#16a34a' })
    // Not posted before `ready` — init will carry it, and nothing is listening.
    expect(sent('theme')).toHaveLength(0)
    expect(host.dataset.theme).toBe('light')
    expect(host.style.getPropertyValue('--canopy-accent')).toBe('#16a34a')

    await fromFrame({ source: SOURCE, type: 'ready' })
    expect(sent('init')[0].message.theme).toMatchObject({ mode: 'light', accent: '#16a34a' })

    widget.setTheme({ mode: 'dark' })
    expect(sent('theme').at(-1)!.message.theme).toEqual({ mode: 'dark' })
    // Replace, not merge: the accent the host dropped is gone, not stuck.
    expect(host.style.getPropertyValue('--canopy-accent')).toBe('')
  })
})

describe('the launcher beside an open docked panel', () => {
  it('hides while the docked panel is open and comes back when it closes', async () => {
    const { widget, root, fromFrame } = widgetHarness({ mode: 'docked', width: 560, expandable: true })
    const dock = root.querySelector('.dock') as HTMLElement
    widget.open()
    expect(dock.dataset.covered).toBe('true')
    // The panel's own × is how a visitor closes a docked rail.
    await fromFrame({ source: SOURCE, type: 'close' })
    expect(widget.isOpen()).toBe(false)
    expect(dock.dataset.covered).toBe('false')
    // Still there to reopen it with.
    expect(root.querySelector('.launcher')).not.toBeNull()
  })

  it('hides when a docked widget opens at once', () => {
    const { root } = widgetHarness({ mode: 'docked', open: true })
    expect((root.querySelector('.dock') as HTMLElement).dataset.covered).toBe('true')
  })

  it('stays beside an open overlay panel, which sits above it', () => {
    const { widget, root } = widgetHarness()
    widget.open()
    expect((root.querySelector('.dock') as HTMLElement).dataset.covered).not.toBe('true')
  })

  it('hides by a rule that beats `.dock { display: flex }`', () => {
    const { root } = widgetHarness({ mode: 'docked' })
    const css = root.querySelector('style')!.textContent!
    expect(css).toMatch(/\.dock\[data-covered="true"\]\s*\{\s*display:\s*none/)
  })
})

describe('the host hiding its launcher for a page', () => {
  it('hides and restores the launcher without the visitor\'s dismiss semantics', () => {
    const { widget, root } = widgetHarness()
    const dock = root.querySelector('.dock') as HTMLElement
    widget.setLauncherVisible(false)
    expect(dock.dataset.suppressed).toBe('true')
    // Not dismissed: that is the visitor's, and lasts the page load.
    expect(widget.isDismissed()).toBe(false)
    widget.setLauncherVisible(true)
    expect(dock.dataset.suppressed).toBe('false')
    // Still in the DOM — reversible, unlike dismiss().
    expect(root.querySelector('.launcher')).not.toBeNull()
  })

  it('closes an open panel rather than strand it with no bubble', () => {
    const { widget } = widgetHarness()
    widget.open()
    widget.setLauncherVisible(false)
    expect(widget.isOpen()).toBe(false)
  })

  it('hides by a rule that beats `.dock { display: flex }`', () => {
    // jsdom does not compute the cascade, so pin the rule itself: the
    // `hidden` attribute LOSES to `.dock`, which is how dismiss() once left the
    // bubble on screen. A class+attribute selector outranks the class alone.
    const { root } = widgetHarness()
    const css = root.querySelector('style')!.textContent!
    expect(css).toMatch(/\.dock\[data-suppressed="true"\]\s*\{\s*display:\s*none/)
  })

  it('is a no-op in inline mode, which has no launcher', () => {
    const target = document.createElement('div')
    document.body.appendChild(target)
    const { widget } = widgetHarness({ mode: 'inline', target })
    expect(() => widget.setLauncherVisible(false)).not.toThrow()
  })
})

describe('ask', () => {
  it('opens the panel and holds a question until the frame has started, then sends it after the page state', async () => {
    const { widget, fromFrame, sent, posted } = widgetHarness()
    widget.setPageState({ resource: 'labs-targeting://pmc' })

    widget.ask('  Explain the answer on this page.  ')

    expect(widget.isOpen()).toBe(true)
    // Nothing is listening yet, so nothing is posted.
    expect(sent('ask')).toHaveLength(0)

    await fromFrame({ source: SOURCE, type: 'ready' })

    expect(sent('ask').map((p) => p.message.text)).toEqual(['Explain the answer on this page.'])
    expect(sent('ask')[0].targetOrigin).toBe(CANOPY)
    // init, then the view, then the question: the conversation it starts sees the page.
    const order = posted.map((p) => p.message.type)
    expect(order.indexOf('init')).toBeLessThan(order.indexOf('state'))
    expect(order.indexOf('state')).toBeLessThan(order.indexOf('ask'))
  })

  it('sends at once to a frame that is already up, and only once', async () => {
    const { widget, fromFrame, sent } = widgetHarness()
    await fromFrame({ source: SOURCE, type: 'ready' })

    widget.ask('Why this schedule?')

    expect(sent('ask').map((p) => p.message.text)).toEqual(['Why this schedule?'])
  })

  it('ignores an empty question and caps a long one', async () => {
    const { widget, fromFrame, sent } = widgetHarness()
    await fromFrame({ source: SOURCE, type: 'ready' })

    widget.ask('   ')
    widget.ask('x'.repeat(5000))

    expect(sent('ask')).toHaveLength(1)
    expect(String(sent('ask')[0].message.text)).toHaveLength(2000)
  })
})

describe('expanding the panel', () => {
  const panelOf = (root: ShadowRoot) => root.querySelector('.panel') as HTMLElement

  it('expands at the frame’s request, answers display, and remembers it per site', async () => {
    const storage = memoryStorage()
    const { widget, root, fromFrame, sent } = widgetHarness({ storage })
    await fromFrame({ source: SOURCE, type: 'ready' })
    expect(sent('init')[0].message.expandable).toBe(true)
    expect(sent('init')[0].message.expanded).toBe(false)

    await fromFrame({ source: SOURCE, type: 'expand', expanded: true })
    expect(widget.isOpen()).toBe(true)
    expect(widget.isExpanded()).toBe(true)
    expect(panelOf(root).dataset.expanded).toBe('true')
    expect(panelOf(root).dataset.mode).toBe('overlay')
    expect(sent('display').at(-1)!.message.expanded).toBe(true)
    expect(storage.data['canopy.widget.display.connect-labs']).toBe('expanded')

    await fromFrame({ source: SOURCE, type: 'expand', expanded: false })
    expect(widget.isExpanded()).toBe(false)
    expect(panelOf(root).dataset.mode).toBe('overlay')
    expect(storage.data['canopy.widget.display.connect-labs']).toBe('normal')
  })

  it('restores a remembered choice over the host default', () => {
    const storage = memoryStorage({ 'canopy.widget.display.connect-labs': 'normal' })
    const { widget } = widgetHarness({ storage, initialDisplay: 'expanded' })
    expect(widget.isExpanded()).toBe(false)
  })

  it('starts expanded when the host says so and nothing is remembered', () => {
    const { widget } = widgetHarness({ initialDisplay: 'expanded', mode: 'docked' })
    expect(widget.isExpanded()).toBe(true)
  })

  it('collapses on Escape back to the mode it came from', () => {
    const { widget, root } = widgetHarness({ mode: 'docked' })
    widget.expand()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    expect(widget.isExpanded()).toBe(false)
    expect(panelOf(root).dataset.mode).toBe('docked')
    expect(widget.isOpen()).toBe(true)
  })

  it('a host call is not remembered as the visitor’s choice', () => {
    const storage = memoryStorage()
    const { widget } = widgetHarness({ storage })
    widget.expand()
    expect(widget.isExpanded()).toBe(true)
    expect(storage.data['canopy.widget.display.connect-labs']).toBeUndefined()
  })

  it('cannot expand when the host turned it off, and says so in init', async () => {
    const { widget, fromFrame, sent } = widgetHarness({ expandable: false })
    await fromFrame({ source: SOURCE, type: 'ready' })
    expect(sent('init')[0].message.expandable).toBe(false)
    await fromFrame({ source: SOURCE, type: 'expand', expanded: true })
    expect(widget.isExpanded()).toBe(false)
  })

  it('inline never expands', () => {
    const slot = document.createElement('div')
    slot.id = 'slot'
    document.body.appendChild(slot)
    const { widget } = widgetHarness({ mode: 'inline', target: '#slot' })
    widget.expand()
    expect(widget.isExpanded()).toBe(false)
  })
})

describe('the conversation open in the panel', () => {
  const ID = '0f8fad5b-d9cb-469f-a165-70867728950e'

  it('openSession waits for the frame, then asks it to open that conversation', async () => {
    const { widget, sent, fromFrame, root } = widgetHarness()
    widget.openSession(ID)
    expect((root.querySelector('.panel') as HTMLElement).dataset.open).toBe('true')
    expect(sent('open-session')).toHaveLength(0)

    await fromFrame({ source: SOURCE, type: 'ready' })
    expect(sent('open-session').map((p) => p.message.id)).toEqual([ID])
    expect(sent('open-session')[0].targetOrigin).toBe(CANOPY)

    widget.openSession(ID)
    expect(sent('open-session')).toHaveLength(2)
  })

  it('ignores an id that is not a canopy session id', async () => {
    const { widget, sent, fromFrame } = widgetHarness()
    await fromFrame({ source: SOURCE, type: 'ready' })
    widget.openSession('../../api/me')
    widget.openSession('')
    expect(sent('open-session')).toHaveLength(0)
  })

  it('reports what the frame says is open: the id, where it began, private — nothing else', async () => {
    const onSession = vi.fn()
    const { widget, fromFrame } = widgetHarness({ onSession })
    expect(widget.currentSession()).toBeNull()

    await fromFrame({
      source: SOURCE, type: 'session',
      session: { id: ID, resource: 'labs-data-space://7', private: true, title: 'leaked words' },
    })
    expect(onSession).toHaveBeenLastCalledWith({ id: ID, resource: 'labs-data-space://7', private: true })
    expect(widget.currentSession()).toEqual({ id: ID, resource: 'labs-data-space://7', private: true })

    await fromFrame({ source: SOURCE, type: 'session', session: null })
    expect(onSession).toHaveBeenLastCalledWith(null)
    expect(widget.currentSession()).toBeNull()
  })

  it('drops a malformed report, and one from anywhere but its own frame', async () => {
    const onSession = vi.fn()
    const { fromFrame } = widgetHarness({ onSession })
    await fromFrame({ source: SOURCE, type: 'session', session: { id: 'nope' } })
    await fromFrame({ source: SOURCE, type: 'session', session: { id: ID } }, { origin: 'https://evil.example' })
    await fromFrame({ source: SOURCE, type: 'session', session: { id: ID } }, { source: {} })
    expect(onSession).not.toHaveBeenCalled()
  })
})
