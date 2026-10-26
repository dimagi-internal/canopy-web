import { describe, expect, it, vi } from 'vitest'

import { createPageGrant, isNoGrant } from './pageGrant'

const GRANT = { id: 1, site: 's', agent: '', agent_name: '', scope: 'x:read',
                granted_at: '2026-10-26T15:00:00Z', expires_at: '2026-10-26T15:30:00Z' }

function world(answers: Array<unknown>) {
  const json = vi.fn(async (_path: string, init?: RequestInit) =>
    init?.method === 'DELETE' ? { grant: null } : { grant: answers.shift() ?? null })
  const invalidateToken = vi.fn()
  const client = { rest: { json }, invalidateToken } as never
  const grant = createPageGrant({ client: () => client, isContact: () => true, pollMs: 1e9 })
  return { grant, json, invalidateToken }
}

describe('the panel act-as-you permission', () => {
  it('asks only once acted, reads it, and stops on Stop', async () => {
    const { grant, json, invalidateToken } = world([GRANT])
    expect(grant.wanted()).toBe(false)
    expect(await grant.ensure()).toBe(true)
    expect(invalidateToken).toHaveBeenCalledTimes(1)
    expect(json).toHaveBeenCalledWith('/api/contact/host-grant')
    expect(grant.current()).toEqual(GRANT)
    expect(await grant.ensure()).toBe(false)

    await grant.stop()
    expect(json).toHaveBeenLastCalledWith('/api/contact/host-grant', { method: 'DELETE' })
    expect(grant.wanted()).toBe(false)
    expect(grant.current()).toBeNull()
    expect(invalidateToken).toHaveBeenCalledTimes(2)
    grant.dispose()
  })

  it('stops asking when a held grant is gone (stopped elsewhere, or run out)', async () => {
    const { grant } = world([GRANT, null])
    await grant.ensure()
    await grant.refresh()
    expect(grant.wanted()).toBe(false)
    grant.dispose()
  })

  it('recognises a View refused for want of a grant', () => {
    expect(isNoGrant({ detail: 'no_grant: connect-labs has not…' })).toBe(true)
    expect(isNoGrant(new Error('not_allowed: no'))).toBe(false)
  })
})
