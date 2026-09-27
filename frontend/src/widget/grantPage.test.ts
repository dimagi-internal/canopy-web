import { describe, expect, it } from 'vitest'

import { grantPageFor, tokenUrlFor } from './grantPage'

/**
 * The keys must match `apps/tokens/self_host.py::PAGE_SCOPES` exactly — a key
 * the server does not know is silently no grant, so a typo here would look
 * like an agent that simply cannot read the page.
 */
describe('which page the widget names when it mints', () => {
  it('names the three pages canopy grants on', () => {
    expect(grantPageFor('/insights')).toBe('insights')
    expect(grantPageFor('/w/connect/agents/ace/inbox')).toBe('agent.inbox')
    expect(grantPageFor('/w/connect/agents/ace/skills/history')).toBe('agent.skill_history')
  })

  it('names nothing anywhere else, so those mints carry no grant', () => {
    for (const path of ['/', '/w/connect', '/w/connect/agents/ace/work', '/w/connect/agents/ace/skills',
      '/w/connect/settings/members', '/insights/extra', '/w/connect/chat/123']) {
      expect(grantPageFor(path)).toBe('')
    }
  })

  it('adds ?page= only when there is a page to name', () => {
    expect(tokenUrlFor('/canopy/api/embed/token', '/insights')).toBe('/canopy/api/embed/token?page=insights')
    expect(tokenUrlFor('/canopy/api/embed/token', '/w/connect')).toBe('/canopy/api/embed/token')
  })
})
