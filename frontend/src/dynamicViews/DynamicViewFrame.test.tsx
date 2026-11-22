// @vitest-environment jsdom
// The canopy page's answers to what a frame asks of it outside the sandbox: a new tab, its URL
// parameters, the clipboard, a download, and the gestures that let a drop write (frameOut.ts, gestures.ts).
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { DynamicViewFrame } from './DynamicViewFrame'
import { CHANNEL, type AppContext } from './protocol'

const SCOPE = { workspace: { slug: 'w', name: 'W' }, project: null, view: { id: 0, slug: 'kanban', title: 'Kanban', version: 0 }, viewer: { email: 'a@example.org', name: 'A' } }
const CONTEXT: AppContext = { key: 'sales', name: 'Sales', page: 'kanban', params: {}, roles: ['member'], public: false, dataModel: {}, settings: {}, fills: {}, draft: false }

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function setup() {
  const setParams = vi.fn()
  const app = { context: CONTEXT, load: async () => ({}), call: async () => null, go: vi.fn(), setParams, canopyReads: true, canConnect: true }
  const view = render(<MemoryRouter><DynamicViewFrame view={{ code: '', sources: [] }} scope={SCOPE} app={app} /></MemoryRouter>)
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
