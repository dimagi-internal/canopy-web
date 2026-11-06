import { afterEach, describe, expect, it, vi } from 'vitest'

import { currentSpecs, runPageAction } from '@/widget/pageActions'
import { currentPageState, hasPageState } from '@/widget/pageState'
import { checkSpec, MAX_ACTIONS, ViewPagePanel } from './pagePanel'

const view = () => ({ slug: 'gmail-clean-up', title: 'Gmail clean-up', workspace: 'marshellis' })

let panel: ViewPagePanel | null = null
afterEach(() => {
  panel?.dispose()
  panel = null
})

describe("a view's side of canopy's panel", () => {
  it('describes the view to the agent, and withdraws it when the view goes', () => {
    panel = new ViewPagePanel(() => {}, view)
    expect(panel.setState({ rows: 3 })).toBeNull()
    expect(currentPageState()).toEqual({
      dynamic_view: { slug: 'gmail-clean-up', title: 'Gmail clean-up', workspace: 'marshellis' },
      rows: 3,
    })
    panel.dispose()
    expect(hasPageState()).toBe(false)
  })

  it('refuses state that is not a plain JSON object', () => {
    panel = new ViewPagePanel(() => {}, view)
    expect(panel.setState([1, 2])).toMatch(/object/)
    expect(panel.setState({ big: 'x'.repeat(70 * 1024) })).toMatch(/limit/)
  })

  it("runs an agent's call in the view and hands back its answer", async () => {
    const sent: Array<[number, string, Record<string, unknown>]> = []
    panel = new ViewPagePanel((id, name, args) => sent.push([id, name, args]), view)
    expect(panel.register({ name: 'setAction', description: 'Change one row', parameters: { type: 'object' } })).toBeNull()
    expect(currentSpecs().map((s) => s.name)).toEqual(['setAction'])

    const call = runPageAction('setAction', { id: 'm1', action: 'keep' })
    expect(sent).toEqual([[1, 'setAction', { id: 'm1', action: 'keep' }]])
    panel.answer(1, true, { changed: 1 }, '')
    await expect(call).resolves.toEqual({ changed: 1 })

    const refused = runPageAction('setAction', {})
    panel.answer(2, false, null, 'no such row')
    await expect(refused).rejects.toThrow('no such row')
  })

  it('times out a view that does not answer, and refuses waiting calls when it goes', async () => {
    vi.useFakeTimers()
    panel = new ViewPagePanel(() => {}, view, 1000)
    panel.register({ name: 'slow' })
    const call = runPageAction('slow', {})
    vi.advanceTimersByTime(1001)
    await expect(call).rejects.toThrow(/did not answer/)
    const waiting = runPageAction('slow', {})
    panel.dispose()
    await expect(waiting).rejects.toThrow(/no longer on screen/)
    expect(currentSpecs()).toEqual([])
    vi.useRealTimers()
  })

  it('checks what a view offers', () => {
    expect(checkSpec({ name: '1bad' })).toMatch(/letter/)
    expect(checkSpec({ name: 'ok', parameters: [] })).toMatch(/JSON-Schema/)
    expect(checkSpec({ name: 'ok', description: 'd' })).toEqual({ name: 'ok', description: 'd' })
    panel = new ViewPagePanel(() => {}, view)
    for (let i = 0; i < MAX_ACTIONS; i++) expect(panel.register({ name: `a${i}` })).toBeNull()
    expect(panel.register({ name: 'one-more' })).toMatch(/at most/)
  })
})
