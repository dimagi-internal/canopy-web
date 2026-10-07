import { describe, expect, it } from 'vitest'

import { grantPageFor, tokenUrlFor } from './grantPage'

/**
 * The keys must match `apps/tokens/self_host.py::PAGE_SCOPES` exactly — a key
 * the server does not know is silently no grant, so a typo here would look
 * like an agent that simply cannot read the page.
 */
describe('which page the widget names when it mints', () => {
  it('names the two pages canopy grants on', () => {
    expect(grantPageFor('/w/connect/agents/ace/tasks')).toBe('agent.tasks')
    expect(grantPageFor('/w/connect/agents/ace/skills/history')).toBe('agent.skill_history')
  })

  it('names nothing anywhere else, so those mints carry no grant', () => {
    for (const path of ['/', '/w/connect', '/w/connect/agents/ace/work', '/w/connect/agents/ace/inbox', '/w/connect/agents/ace/skills',
      '/w/connect/settings/members', '/w/connect/agents/ace/tasks/extra', '/w/connect/chat/123',
      // The retired Insights feed: no longer a grant page.
      '/insights']) {
      expect(grantPageFor(path)).toBe('')
    }
  })

  it('adds ?page= only when there is a page to name', () => {
    expect(tokenUrlFor('/canopy/api/embed/token', '/w/connect/agents/ace/tasks')).toBe('/canopy/api/embed/token?page=agent.tasks')
    expect(tokenUrlFor('/canopy/api/embed/token', '/w/connect')).toBe('/canopy/api/embed/token')
  })
})
