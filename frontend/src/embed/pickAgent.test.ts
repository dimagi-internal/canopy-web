import { describe, expect, it } from 'vitest'

import { pickInitialAgent } from './pickAgent'

const a = (slug: string, isDefault = false) => ({ slug, default: isDefault })

describe('pickInitialAgent', () => {
  it("opens on canopy's default for this visitor", () => {
    expect(pickInitialAgent([a('ada'), a('trellis', true)])?.slug).toBe('trellis')
  })

  it("ranks canopy's default above the agent the host named", () => {
    // An SDK host sends its AGENT_SLUG fallback as `init.agent`; a page that
    // really chose its agent bound the token instead, so the list is just that one.
    expect(pickInitialAgent([a('ada', true), a('trellis')], 'trellis')?.slug).toBe('ada')
  })

  it('falls back to the agent the host named', () => {
    expect(pickInitialAgent([a('ada'), a('trellis')], 'trellis')?.slug).toBe('trellis')
  })

  it('ignores a named agent this visitor is not offered', () => {
    expect(pickInitialAgent([a('ada'), a('trellis')], 'labsdata')).toBeNull()
  })

  it('opens on the only agent on offer', () => {
    expect(pickInitialAgent([a('ada')])?.slug).toBe('ada')
  })

  it('leaves several, with no default, to the picker', () => {
    expect(pickInitialAgent([a('ada'), a('trellis')])).toBeNull()
  })
})
