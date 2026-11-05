import { describe, expect, it } from 'vitest'

import type { AccessRoleOut } from '@/api/agents'
import { assignable, drives, roleWord } from './accessWords'

describe('roleWord', () => {
  it('names every role on the ladder, the agent itself, and no access, and never says bare "admin"', () => {
    expect(['owner', 'admin', 'editor', 'user', 'viewer'].map((r) => roleWord(r))).toEqual([
      'Owner', 'Agent admin', 'Editor', 'User', 'Viewer',
    ])
    expect(roleWord('system')).toBe('The agent itself')
    expect(roleWord(null)).toBe('No access')
  })
})

const ROLE = (role: string, label: string, contains_write = false, fixed = false): AccessRoleOut => ({
  role, label, can: [], permissions: [], description: '', contains_write, defined_on: fixed ? '' : 'dimagi', fixed,
})

describe('roles from the server', () => {
  const roles = [ROLE('owner', 'Owner', true, true), ROLE('admin', 'Admin', true, true), ROLE('pilot', 'Pilot', true),
    ROLE('connector', 'Connect site'), ROLE('read_only', 'Read only')]

  it('offers the roles a line may give, weakest first, never the fixed ones', () => {
    expect(assignable(roles).map((r) => r.role)).toEqual(['read_only', 'connector', 'pilot'])
  })

  it('says whether a role drives from what the server sent, never from its name', () => {
    expect(drives('pilot', roles)).toBe(true)
    expect(drives('connector', roles)).toBe(false)
    expect(drives('editor', roles)).toBe(false)
  })

  it('reads a role as its label, and a fixed admin as "Agent admin"', () => {
    expect(roleWord('connector', roles)).toBe('Connect site')
    expect(roleWord('admin', roles)).toBe('Agent admin')
    expect(roleWord('mystery', roles)).toBe('Mystery')
  })
})
