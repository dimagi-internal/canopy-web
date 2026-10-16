// GET /api/vault/keyset answers 404 `no_vault_keyset` until set up; the client
// turns that into `exists: false` with the setup fields the 404 carries.
import { describe, expect, it, vi } from 'vitest'

const { GET } = vi.hoisted(() => ({ GET: vi.fn() }))
vi.mock('./client.v2', () => ({ apiV2: { GET } }))
const { getKeyset } = await import('./vault')

describe('getKeyset', () => {
  it('reads a 404 no_vault_keyset as not set up', async () => {
    GET.mockResolvedValue({ response: { status: 404 }, error: {
      error: 'no_vault_keyset', user_id: '7', prf_input: 'cHJm', passkeys: [{ credential_id: 'c', label: '', sealed: false }] } })
    expect(await getKeyset()).toMatchObject({ exists: false, user_id: '7', prf_input: 'cHJm', seals: [] })
  })

  it('throws on any other failure', async () => {
    GET.mockResolvedValue({ response: { status: 500 }, error: { detail: 'boom' } })
    await expect(getKeyset()).rejects.toThrow()
  })
})
