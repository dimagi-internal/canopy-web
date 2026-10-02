import { describe, it, expect } from 'vitest'
// The backend table, read as source so the two cannot drift: a capability
// added or moved in permissions.py without the same edit here fails this test.
import permissionsSrc from '../../../apps/workspaces/permissions.py?raw'
import { MINIMUM_ROLE, grantableRoles, mayManageMember, roleAllows } from './workspaceRoles'

function backendTable(): Record<string, string> {
  const consts: Record<string, string> = {}
  for (const m of permissionsSrc.matchAll(/^([A-Z_]+) = "([a-z._]+)"$/gm)) consts[m[1]] = m[2]
  const block = permissionsSrc.split('MINIMUM_ROLE: dict[str, str] = {')[1].split('}')[0]
  const out: Record<string, string> = {}
  for (const m of block.matchAll(/([A-Z_]+): _M\.([A-Z]+),/g)) out[consts[m[1]]] = m[2].toLowerCase()
  return out
}

describe('workspaceRoles', () => {
  it('mirrors apps/workspaces/permissions.py exactly', () => {
    expect(MINIMUM_ROLE).toEqual(backendTable())
  })

  it('gives admin the logs and members, and the keys only to owners', () => {
    expect(roleAllows('admin', 'logs.read')).toBe(true)
    expect(roleAllows('editor', 'logs.read')).toBe(false)
    expect(roleAllows('admin', 'own')).toBe(false)
    expect(roleAllows(undefined, 'read')).toBe(false)
  })

  it('lets an admin manage only below themselves', () => {
    expect(mayManageMember('admin', 'editor', 'viewer')).toBe(true)
    expect(mayManageMember('admin', 'admin')).toBe(false)
    expect(mayManageMember('admin', 'editor', 'admin')).toBe(false)
    expect(grantableRoles('admin')).toEqual(['viewer', 'editor'])
    expect(grantableRoles('owner')).toEqual(['viewer', 'editor', 'admin', 'owner'])
    expect(grantableRoles('editor')).toEqual([])
  })
})
