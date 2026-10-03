import { describe, expect, it } from 'vitest'
import { agentHref } from './agentHref'

describe('agentHref', () => {
  it("links to the agent's home workspace, not the active one", () => {
    expect(agentHref({ slug: 'ada', workspace: 'dimagi' })).toBe('/w/dimagi/agents/ada')
  })

  it('falls back to the flat route when the workspace is unknown', () => {
    expect(agentHref({ slug: 'ada', workspace: null })).toBe('/agents/ada')
  })
})
