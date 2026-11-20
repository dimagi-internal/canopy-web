import { describe, expect, it, vi } from 'vitest'
import { BrowserError, BrowserGate, ExtensionLink, REQUEST_TIMEOUT_MS, allowedOrigins, visibleContext, type BrowserDeclaration } from './extensionBridge'

const EXT = 'chrome-extension://lkbpnpoflndilnhefmhjpmdibdcopcfo'
const OTHER = 'chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
const GMAIL = 'https://mail.google.com/mail/u/0/#inbox/abc'
const parent = { name: 'the extension panel' }

function setup(opts: { ancestor?: string | null; allowed?: string[] } = {}) {
  const posts: { msg: Record<string, unknown>; origin: string }[] = []
  const timers: (() => void)[] = []
  const handlers = { context: vi.fn(), hello: vi.fn(), event: vi.fn() }
  let n = 0
  const link = new ExtensionLink(opts.allowed ?? [EXT], {
    parent,
    ancestor: opts.ancestor === undefined ? EXT : opts.ancestor,
    post: (msg, origin) => posts.push({ msg, origin }),
    setTimeout: (fn) => { timers.push(fn); return timers.length },
    clearTimeout: () => {},
    newId: () => `id-${++n}`,
  }, handlers)
  const from = (data: unknown, origin = EXT, source: unknown = parent) => link.receive({ source, origin, data })
  return { link, posts, timers, handlers, from }
}

const ctx = (url: string | null, id = 7) => ({
  type: 'canopy-ext:context', v: 1, tab: url ? { id, url, title: 'Inbox' } : null, adapters: [{ name: 'gmail', ready: true }],
})

describe('ExtensionLink: the panel ⇄ extension hop', () => {
  it('posts ready only to the allowed extension that framed it', () => {
    const { link, posts } = setup()
    link.ready()
    expect(posts).toEqual([{ msg: { type: 'canopy-ext:ready', v: 1 }, origin: EXT }])
  })

  it('without the browser naming its parent, offers each allowed origin, then locks to the one that answers', () => {
    const { link, posts, from } = setup({ ancestor: null, allowed: [EXT, OTHER] })
    link.ready()
    expect(posts.map((p) => p.origin)).toEqual([EXT, OTHER])
    expect(from(ctx(GMAIL), OTHER)).toBe(true)
    expect(link.extensionOrigin).toBe(OTHER)
    expect(from(ctx(GMAIL), EXT)).toBe(false) // the other allowed origin is no longer heard
  })

  it('ignores a parent that is not an allowed extension, and an allowed origin that is not the parent', () => {
    const { link, from } = setup({ ancestor: 'https://evil.example' })
    expect(link.extensionOrigin).toBeNull()
    expect(from(ctx(GMAIL), 'https://evil.example')).toBe(false)
    expect(from(ctx(GMAIL), EXT, { name: 'some other window' })).toBe(false)
    expect(from(ctx(GMAIL), OTHER)).toBe(false)
    expect(link.context).toBeNull()
  })

  it('ignores a message of another protocol or version', () => {
    const { from, handlers } = setup()
    expect(from({ ...ctx(GMAIL), v: 2 })).toBe(false)
    expect(from({ ...ctx(GMAIL), type: 'context' })).toBe(false)
    expect(handlers.context).not.toHaveBeenCalled()
  })

  it('answers a request by its id, and only that id', async () => {
    const { link, posts, from } = setup()
    const p = link.request(7, 'gmail', 'read_open_email', {})
    expect(posts.at(-1)).toEqual({ msg: { type: 'canopy-ext:request', v: 1, id: 'id-1', tabId: 7, adapter: 'gmail', capability: 'read_open_email', args: {} }, origin: EXT })
    from({ type: 'canopy-ext:response', v: 1, id: 'id-999', ok: true, result: 'wrong' })
    from({ type: 'canopy-ext:response', v: 1, id: 'id-1', ok: true, result: { subject: 'Hi' } })
    await expect(p).resolves.toEqual({ subject: 'Hi' })
  })

  it('carries the extension\'s error code', async () => {
    const { link, from } = setup()
    const p = link.request(7, 'gmail', 'read_open_email', {})
    from({ type: 'canopy-ext:response', v: 1, id: 'id-1', ok: false, error: { code: 'no_adapter', message: 'no gmail here' } })
    await expect(p).rejects.toMatchObject({ code: 'no_adapter', message: 'no gmail here' })
  })

  it(`times out after ${REQUEST_TIMEOUT_MS / 1000} s`, async () => {
    const { link, timers } = setup()
    const p = link.request(7, 'gmail', 'read_open_email', {})
    timers[0]()
    await expect(p).rejects.toMatchObject({ code: 'timeout' })
  })

  it('opens https only', () => {
    const { link, posts } = setup()
    link.open('https://login.example/x')
    expect(posts.at(-1)).toEqual({ msg: { type: 'canopy-ext:open', v: 1, url: 'https://login.example/x' }, origin: EXT })
    for (const bad of ['javascript:alert(1)', 'http://x.example/', 'chrome://settings', 'nope']) {
      expect(() => link.open(bad)).toThrow(BrowserError)
    }
  })
})

const DECL: BrowserDeclaration = {
  adapter: 'gmail', capabilities: ['read_open_email'], events: ['email_opened'], matches: ['https://mail.google.com/*'],
}

describe('BrowserGate: the app page ⇄ panel hop', () => {
  function gated(url: string | null) {
    const s = setup()
    s.from(ctx(url))
    return { ...s, gate: new BrowserGate(DECL, s.link) }
  }

  it('relays a declared capability on a matching tab', () => {
    const { gate, posts } = gated(GMAIL)
    void gate.request('read_open_email', {})
    expect(posts.at(-1)?.msg).toMatchObject({ type: 'canopy-ext:request', tabId: 7, adapter: 'gmail', capability: 'read_open_email' })
  })

  it('refuses an undeclared capability before anything is sent', async () => {
    const { gate, posts } = gated(GMAIL)
    const before = posts.length
    await expect(gate.request('compose_recipients', {})).rejects.toMatchObject({ code: 'unknown_capability' })
    expect(posts.length).toBe(before)
  })

  it('refuses when the tab in front does not match the page', async () => {
    for (const url of ['https://evil.example/', 'http://mail.google.com/', null]) {
      const { gate, posts } = gated(url)
      const before = posts.length
      await expect(gate.request('read_open_email', {})).rejects.toMatchObject({ code: 'no_adapter' })
      expect(posts.length).toBe(before)
    }
  })

  it('refuses args that are not an object', async () => {
    const { gate } = gated(GMAIL)
    await expect(gate.request('read_open_email', 'x')).rejects.toMatchObject({ code: 'bad_request' })
  })

  it('shows the page the tab only while it matches', () => {
    expect(gated(GMAIL).gate.context()).toEqual({ tab: { url: GMAIL, title: 'Inbox' } })
    expect(gated('https://evil.example/').gate.context()).toBeNull()
    expect(visibleContext(DECL, null)).toBeNull()
  })

  it('admits only a declared event, from its adapter, about the tab in front', () => {
    const { gate } = gated(GMAIL)
    expect(gate.admits({ tabId: 7, adapter: 'gmail', event: 'email_opened', data: {} })).toBe(true)
    expect(gate.admits({ tabId: 7, adapter: 'gmail', event: 'email_sent', data: {} })).toBe(false)
    expect(gate.admits({ tabId: 8, adapter: 'gmail', event: 'email_opened', data: {} })).toBe(false)
    expect(gate.admits({ tabId: 7, adapter: 'outlook', event: 'email_opened', data: {} })).toBe(false)
    expect(gated('https://evil.example/').gate.admits({ tabId: 7, adapter: 'gmail', event: 'email_opened', data: {} })).toBe(false)
  })
})

describe('allowedOrigins', () => {
  it('reads the server\'s meta, extension origins only', () => {
    const doc = { querySelector: () => ({ getAttribute: () => JSON.stringify([EXT, 'https://evil.example', 3]) }) }
    expect(allowedOrigins(doc as unknown as Document)).toEqual([EXT])
    expect(allowedOrigins({ querySelector: () => null } as unknown as Document)).toEqual([])
    expect(allowedOrigins({ querySelector: () => ({ getAttribute: () => '{bad' }) } as unknown as Document)).toEqual([])
  })
})
