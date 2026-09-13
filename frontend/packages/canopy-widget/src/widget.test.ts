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
function widgetHarness(overrides: Record<string, unknown> = {}) {
  const posted: Array<{ message: Record<string, unknown>; targetOrigin: string }> = []

  const widget = init({
    baseUrl: CANOPY,
    app: 'connect-labs',
    tokenUrl: TOKEN_URL,
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

  it('sandboxes the frame without letting it navigate the host page', () => {
    const { iframe } = widgetHarness()
    const sandbox = iframe.getAttribute('sandbox') ?? ''
    expect(sandbox).toContain('allow-scripts')
    expect(sandbox).toContain('allow-same-origin')
    expect(sandbox).not.toContain('allow-top-navigation')
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

  it('sends the CSRF header when the host uses a csrftoken cookie', async () => {
    document.cookie = 'csrftoken=abc123'
    const { fromFrame } = widgetHarness()
    await fromFrame({ source: SOURCE, type: 'ready' })

    const [, init_] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(init_.headers['X-CSRFToken']).toBe('abc123')
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

  it('declares the action list on init', async () => {
    const { widget, fromFrame, sent } = widgetHarness()
    widget.registerAction('b', vi.fn())
    widget.registerAction('a', vi.fn())

    await fromFrame({ source: SOURCE, type: 'ready' })

    expect(sent('init')[0].message.actions).toEqual(['a', 'b'])
  })

  it('tells an open frame when the action set changes', async () => {
    const { widget, sent } = widgetHarness()
    widget.registerAction('a', vi.fn())
    expect(sent('actions')[0].message.actions).toEqual(['a'])

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
