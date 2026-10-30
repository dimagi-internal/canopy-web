import { describe, expect, it } from 'vitest'

import { roleWord } from './accessWords'

describe('roleWord', () => {
  it('names every role on the ladder, the agent itself, and no access, and never says bare "admin"', () => {
    expect(['owner', 'admin', 'editor', 'user', 'viewer'].map(roleWord)).toEqual([
      'Owner', 'Agent admin', 'Editor', 'User', 'Viewer',
    ])
    expect(roleWord('system')).toBe('The agent itself')
    expect(roleWord(null)).toBe('No access')
  })
})
