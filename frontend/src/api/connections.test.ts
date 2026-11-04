import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from './problem'
import type { ShareIn } from './connections'

const GET = vi.fn()
const PUT = vi.fn()
vi.mock('./client.v2', () => ({ apiV2: { GET, PUT } }))

const connections = await import('./connections')

const ID = '0f8e3c2a-5b1d-4e7f-9a60-3c2b1d0e9f87'
const path = { params: { path: { connection_id: ID } } }

afterEach(() => vi.clearAllMocks())

describe('sharing a connection', () => {
  it('reads what sharing needs', async () => {
    GET.mockResolvedValue({ data: { id: ID, state: 'unshared' }, response: { ok: true } })
    expect(await connections.getShareState(ID)).toEqual({ id: ID, state: 'unshared' })
    expect(GET).toHaveBeenCalledWith('/api/connections/{connection_id}/share', path)
  })

  it('writes the share and answers the connection', async () => {
    const body = { statement: {}, sig: 's', by_jkt: 'j', sealed: 'zks1:x', sealed_sig: {}, expect: 'e' } as ShareIn
    PUT.mockResolvedValue({ data: { id: ID, state: 'active' }, response: { ok: true } })
    expect(await connections.putShare(ID, body)).toEqual({ id: ID, state: 'active' })
    expect(PUT).toHaveBeenCalledWith('/api/connections/{connection_id}/share', { ...path, body })
  })

  it('carries a refusal’s code to the caller', async () => {
    PUT.mockResolvedValue({
      error: { detail: 'Reload and share again.', error: 'statement_stale' },
      response: { ok: false, status: 409 },
    })
    const e = await connections.putShare(ID, {} as ShareIn).catch((x: unknown) => x)
    expect(e).toBeInstanceOf(ApiError)
    expect(e).toMatchObject({ status: 409, code: 'statement_stale', message: 'Reload and share again.' })
  })

  it('has no per-agent grant calls left', () => {
    expect('grantConnection' in connections).toBe(false)
    expect('revokeConnectionGrant' in connections).toBe(false)
  })
})
