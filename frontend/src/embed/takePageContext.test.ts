import { describe, expect, it } from 'vitest'
import { takePageContext } from './EmbedChat'

const ref = (v: string | null) => ({ current: v })

describe('takePageContext', () => {
  it('appends the snapshot when the host only lends context', () => {
    const pre = ref('Context from the page I am on: …')
    expect(takePageContext({ pageState: () => null }, pre)).toBe('Context from the page I am on: …')
    expect(pre.current).toBeNull()
  })

  it('drops the snapshot when the host declares page state — the envelope carries it', () => {
    const pre = ref('Context from the page I am on: …')
    const declared = { pageState: () => ({ resource: 'ide.sessions', visible_ids: [] }) }
    expect(takePageContext(declared as never, pre)).toBeNull()
    // consumed either way, so a later send cannot pick it up
    expect(pre.current).toBeNull()
  })
})
