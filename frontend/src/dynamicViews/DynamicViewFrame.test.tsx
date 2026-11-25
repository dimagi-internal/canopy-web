// @vitest-environment jsdom
// The canopy page's answers to what a frame asks of it outside the sandbox: a new tab, its URL
// parameters, the clipboard, a download, and the gestures that let a drop write (frameOut.ts, gestures.ts).
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { DynamicViewFrame, type BrowserHost } from './DynamicViewFrame'
import { BrowserGate } from '@/ext/extensionBridge'
import { ModeStore } from '@/ext/modeStore'
import { CHANNEL, type AppContext } from './protocol'

const SCOPE = { workspace: { slug: 'w', name: 'W' }, project: null, view: { id: 0, slug: 'kanban', title: 'Kanban', version: 0 }, viewer: { email: 'a@example.org', name: 'A' } }
const CONTEXT: AppContext = { key: 'sales', name: 'Sales', page: 'kanban', params: {}, roles: ['member'], public: false, dataModel: {}, settings: {}, fills: {}, draft: false }

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function setup(browser?: BrowserHost) {
  const setParams = vi.fn()
  const app = { context: CONTEXT, load: async () => ({}), call: async () => null, go: vi.fn(), setParams, canopyReads: true, canConnect: true }
  const view = render(<MemoryRouter><DynamicViewFrame view={{ code: '', sources: [] }} scope={SCOPE} app={app} browser={browser} /></MemoryRouter>)
  const frame = view.container.querySelector('iframe')!
  const answers: unknown[] = []
  vi.spyOn(frame.contentWindow!, 'postMessage').mockImplementation((msg: unknown) => void answers.push(msg))
  const send = async (data: Record<string, unknown>) => {
    await act(async () => {
      window.dispatchEvent(new MessageEvent('message', { data: { channel: CHANNEL, ...data }, source: frame.contentWindow }))
      await Promise.resolve()
    })
  }
  const answer = (id: number) => answers.find((a) => (a as { id?: number }).id === id) as { ok: boolean; error?: string; data?: unknown } | undefined
  return { frame, send, answer, setParams }
}

/** A live click on this frame, as the page reads it (`pageClickedFrame`). */
function click(frame: HTMLIFrameElement, on = true) {
  Object.defineProperty(navigator, 'userActivation', { value: { isActive: on }, configurable: true })
  vi.spyOn(document, 'hasFocus').mockReturnValue(on)
  Object.defineProperty(document, 'activeElement', { value: on ? frame : document.body, configurable: true })
}

describe('DynamicViewFrame outside the frame', () => {
  it('opens only an https address, only from a click, never with an opener', async () => {
    const { frame, send, answer } = setup()
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    await send({ type: 'open-url', id: 1, url: 'javascript:alert(1)' })
    expect(answer(1)).toMatchObject({ ok: false, error: 'canopy.open opens only an https address' })
    click(frame, false)
    await send({ type: 'open-url', id: 2, url: 'https://dimagi.my.salesforce.com/00Q1' })
    expect(answer(2)).toMatchObject({ ok: false, error: 'This needs a click.' })
    click(frame)
    await send({ type: 'open-url', id: 3, url: 'https://dimagi.my.salesforce.com/00Q1' })
    expect(answer(3)).toMatchObject({ ok: true })
    expect(open).toHaveBeenCalledWith('https://dimagi.my.salesforce.com/00Q1', '_blank', 'noopener,noreferrer')
  })

  it('writes page params through the host, checked', async () => {
    const { send, answer, setParams } = setup()
    await send({ type: 'page-params', id: 1, params: { board: '7' } })
    expect(setParams).toHaveBeenCalledWith({ board: '7' })
    expect(answer(1)).toMatchObject({ ok: true, data: { board: '7' } })
    await send({ type: 'page-params', id: 2, params: { draft: '1' } })
    expect(answer(2)).toMatchObject({ ok: false })
    expect(setParams).toHaveBeenCalledTimes(1)
  })

  it('a download needs a click, or a gesture the page believed', async () => {
    const { frame, send, answer } = setup()
    URL.createObjectURL = vi.fn(() => 'blob:x')
    URL.revokeObjectURL = vi.fn()
    click(frame, false)
    await send({ type: 'download', id: 1, name: 'export.csv', mime: 'text/csv', data: 'a,b' })
    expect(answer(1)).toMatchObject({ ok: false, error: 'This needs a click.' })
    // A gesture message with no live click is not believed...
    await send({ type: 'gesture' })
    await send({ type: 'download', id: 2, name: 'export.csv', mime: 'text/csv', data: 'a,b' })
    expect(answer(2)).toMatchObject({ ok: false })
    // ...one with a live click is, after the click's activation has ended.
    click(frame)
    await send({ type: 'gesture' })
    click(frame, false)
    await send({ type: 'download', id: 3, name: 'export.csv', mime: 'text/csv', data: 'a,b' })
    expect(answer(3)).toMatchObject({ ok: true, data: { name: 'export.csv' } })
  })

  it('copies text from a click', async () => {
    const { frame, send, answer } = setup()
    const writeText = vi.fn(async () => undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    click(frame, false)
    await send({ type: 'clipboard', id: 1, text: 'hi' })
    expect(answer(1)).toMatchObject({ ok: false, error: 'This needs a click.' })
    click(frame)
    await send({ type: 'clipboard', id: 2, text: 'hi' })
    await act(async () => { await Promise.resolve() })
    expect(writeText).toHaveBeenCalledWith('hi')
    expect(answer(2)).toMatchObject({ ok: true })
  })
})

describe('canopy.browser in the extension panel', () => {
  // The panel's real gate, over a link that has a Gmail tab in front and answers every request.
  function panel() {
    const sent: string[] = []
    const link = {
      context: { tab: { id: 7, url: 'https://mail.google.com/mail/u/0/', title: 'Inbox' }, adapters: [] },
      request: vi.fn(async (_tab: number, _adapter: string, capability: string) => { sent.push(capability); return { ok: capability } }),
      open: vi.fn(),
    }
    const gate = new BrowserGate({
      adapter: 'gmail', capabilities: ['read_open_thread', 'fill_compose'], acts: ['fill_compose'], events: [],
      matches: ['https://mail.google.com/*'],
    }, link)
    const host: BrowserHost = {
      info: { adapter: 'gmail', capabilities: ['read_open_thread', 'fill_compose'], acts: ['fill_compose'], events: [] },
      context: () => gate.context(),
      request: (c, a, clicked) => gate.request(c, a, clicked),
      open: (u) => gate.open(u),
      subscribe: () => () => undefined,
      mode: new ModeStore('test', null),
    }
    return { sent, link, host }
  }
  const settle = () => act(async () => { await Promise.resolve(); await Promise.resolve() })

  it('an act needs a click on this frame, read by the page; a read does not', async () => {
    const { sent, link, host } = panel()
    const { frame, send, answer } = setup(host)
    click(frame, false)
    await send({ type: 'browser', id: 1, request: { op: 'request', capability: 'fill_compose', args: { body: 'hi' } } })
    await settle()
    expect(answer(1)).toMatchObject({ ok: false, error: 'This needs a click.', code: 'needs_click' })
    await send({ type: 'browser', id: 2, request: { op: 'request', capability: 'read_open_thread', args: {} } })
    await settle()
    expect(answer(2)).toMatchObject({ ok: true })
    click(frame)
    await send({ type: 'browser', id: 3, request: { op: 'request', capability: 'fill_compose', args: { body: 'hi' } } })
    await settle()
    expect(answer(3)).toMatchObject({ ok: true })
    expect(sent).toEqual(['read_open_thread', 'fill_compose'])
    expect(link.open).not.toHaveBeenCalled()
  })

  it('canopy.google runs in the person\'s mode, through the same gate, and says which', async () => {
    const { sent, host } = panel()
    const { frame, send, answer } = setup(host)
    click(frame, false)
    // Server mode (the default): a local-only operation is refused, and the tab is not touched.
    await send({ type: 'google', id: 1, op: 'gmail.fill_compose', args: { bodyText: 'hi' } })
    await settle()
    expect(answer(1)).toMatchObject({ ok: false, code: 'not_in_mode' })
    // Switching needs a click; then the same call goes to the tab, and still needs a click to act.
    await send({ type: 'mode-set', id: 2, mode: 'local' })
    expect(answer(2)).toMatchObject({ ok: false, code: 'needs_click' })
    click(frame)
    await send({ type: 'mode-set', id: 3, mode: 'local' })
    expect(answer(3)).toMatchObject({ ok: true, data: { current: 'local', available: ['local', 'server'] } })
    click(frame, false)
    await send({ type: 'google', id: 4, op: 'gmail.fill_compose', args: { bodyText: 'hi' } })
    await settle()
    expect(answer(4)).toMatchObject({ ok: false, code: 'needs_click' })
    click(frame)
    await send({ type: 'google', id: 5, op: 'gmail.fill_compose', args: { bodyText: 'hi' } })
    await settle()
    expect(answer(5)).toMatchObject({ ok: true })
    expect(sent).toEqual(['fill_compose'])
  })

  it('open() without a click is needs_click too', async () => {
    const { host } = panel()
    const { frame, send, answer } = setup(host)
    click(frame, false)
    await send({ type: 'browser', id: 1, request: { op: 'open', url: 'https://example.org/' } })
    expect(answer(1)).toMatchObject({ ok: false, error: 'This needs a click.', code: 'needs_click' })
  })
})
