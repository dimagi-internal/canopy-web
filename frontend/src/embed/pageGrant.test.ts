import { describe, expect, it, vi } from 'vitest'

import { createPageGrant, isNoGrant } from './pageGrant'

const GRANT = { id: 1, site: 's', agent: '', agent_name: '', scope: 'x:read',
                granted_at: '2026-10-26T15:00:00Z', expires_at: '2026-10-26T15:30:00Z',
                confirmed: true }

function world(answers: Array<unknown>, isContact = false) {
  const json = vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === 'DELETE') return { grant: null }
    if (path.endsWith('/confirm/begin')) return { options: { challenge: 'AAAA', allowCredentials: [] } }
    return { grant: answers.shift() ?? null }
  })
  const invalidateToken = vi.fn()
  const client = { rest: { json }, invalidateToken } as never
  const grant = createPageGrant({ client: () => client, isContact: () => isContact, pollMs: 1e9 })
  return { grant, json, invalidateToken }
}

describe('the panel act-as-you permission', () => {
  it('asks only once acted, reads it, and stops on Stop', async () => {
    const { grant, json, invalidateToken } = world([GRANT])
    expect(grant.wanted()).toBe(false)
    expect(await grant.ensure()).toBe(true)
    expect(invalidateToken).toHaveBeenCalledTimes(1)
    expect(json).toHaveBeenCalledWith('/api/embed/host-grant')
    expect(grant.current()).toEqual(GRANT)
    expect(await grant.ensure()).toBe(false)

    await grant.stop()
    expect(json).toHaveBeenLastCalledWith('/api/embed/host-grant', { method: 'DELETE' })
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

  it('never asks for a contact, who has no passkey', async () => {
    const { grant, json, invalidateToken } = world([GRANT], true)
    expect(await grant.ensure()).toBe(false)
    expect(grant.wanted()).toBe(false)
    expect(invalidateToken).not.toHaveBeenCalled()
    expect(json).not.toHaveBeenCalled()
    grant.dispose()
  })

  it('confirms a waiting grant with the passkey, then holds the confirmed one', async () => {
    const waiting = { ...GRANT, confirmed: false }
    const { grant, json } = world([waiting, GRANT])
    const credential = {
      id: 'cred', rawId: new Uint8Array([1]).buffer, type: 'public-key',
      response: { clientDataJSON: new Uint8Array([2]).buffer, authenticatorData: new Uint8Array([3]).buffer,
                  signature: new Uint8Array([4]).buffer },
      getClientExtensionResults: () => ({}),
    }
    const get = vi.fn(async () => credential)
    vi.stubGlobal('navigator', { credentials: { get } })
    await grant.ensure()
    expect(grant.current()?.confirmed).toBe(false)
    await grant.confirm()
    expect(get).toHaveBeenCalledTimes(1)
    expect(json).toHaveBeenCalledWith('/api/embed/host-grant/confirm/begin', { method: 'POST' })
    const finish = json.mock.calls.find(([p]) => p === '/api/embed/host-grant/confirm/finish')
    expect(JSON.parse(String(finish?.[1]?.body)).credential.id).toBe('cred')
    expect(grant.current()).toEqual(GRANT)
    vi.unstubAllGlobals()
    grant.dispose()
  })

  it('recognises a View refused for want of a grant', () => {
    expect(isNoGrant({ detail: 'no_grant: connect-labs has not…' })).toBe(true)
    expect(isNoGrant(new Error('not_allowed: no'))).toBe(false)
  })
})
