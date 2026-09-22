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

  it('works with no storage at all — dragging is not a storage feature', () => {
    const { root } = widgetHarness({ storage: null })
    const dock = root.querySelector('.dock') as HTMLElement

    drag(root.querySelector('.launcher')!, [500, 500], [200, 300])

    expect(dock.dataset.moved).toBe('true')
  })
})

describe('the page state channel', () => {
  it('pushes the view to an open frame, on its own message', async () => {
    const { widget, fromFrame, sent } = widgetHarness()
    await fromFrame({ source: SOURCE, type: 'ready' })

    widget.setPageState({ visible_ids: [4471, 4472], backing_tool: 'list_insights' })

    const [push] = sent('state')
    expect(push.message.state).toEqual({
      visible_ids: [4471, 4472],
      backing_tool: 'list_insights',
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
