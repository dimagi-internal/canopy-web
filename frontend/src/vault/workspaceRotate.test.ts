// Rotating a workspace vault's key, with the REAL crypto module: what the browser sends
// is opened with the keys only the person and the box hold — a fresh key at kv+1, a
// copy for each holder still current and each one waiting, every value re-encrypted.
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { PublicJwk } from './zk'

const api = vi.hoisted(() => ({
  getKeyset: vi.fn(), getWorkspaceVaultZk: vi.fn(), rotateWorkspaceVaultZk: vi.fn(),
}))
vi.mock('@/api/vault', () => api)

const zk = await import('./zk')
const { rotateWorkspaceVaultKey } = await import('./operations')

const VAULT = '7f6c0c55-0000-4000-8000-000000000001'
const BINDING = `vault:${VAULT}`
const ME = '7'

afterEach(() => vi.clearAllMocks())

async function vaultAt(kv: number) {
  const me = await zk.generateKeyPair()
  const box = await zk.generateKeyPair()
  const gone = await zk.generateKeyPair()
  const vk = zk.randomBytes(32)
  const boxJkt = await zk.jkt(box.publicJwk)
  const wrap = async (pub: PublicJwk, info: string) => zk.wrapEcdhEs(vk, pub, info)
  const state = {
    agent: 'Acme shared', agent_id: BINDING, enabled: true, kv,
    wraps: [
      { kind: 'member', kv, target: ME, public_jwk: me.publicJwk, current: true,
        wrap: await wrap(me.publicJwk, zk.memberInfo(BINDING, kv, ME)) },
      // Removed from the vault since: it still holds a copy of THIS key.
      { kind: 'member', kv, target: '9', public_jwk: gone.publicJwk, current: false,
        wrap: await wrap(gone.publicJwk, zk.memberInfo(BINDING, kv, '9')) },
    ],
    pending: [{ kind: 'device', target: 'dev-1', public_jwk: box.publicJwk, ecdh_jkt: boxJkt }],
    values: { 'gog-token': await zk.encryptValue(vk, BINDING, 'gog-token', kv, 'tok-value') },
    rotation_recommended: true, sealed: [],
  }
  return { state, me: { userId: ME, privateKey: me.privateKey, publicJwk: me.publicJwk }, box, boxJkt }
}

describe('rotateWorkspaceVaultKey', () => {
  it('re-keys the vault for everyone still in it, and leaves the removed member out', async () => {
    const { state, me, box, boxJkt } = await vaultAt(3)
    api.getKeyset.mockResolvedValue({ exists: true, seals: [{ alg: 'recovery' }] })
    api.getWorkspaceVaultZk.mockResolvedValue(state)
    api.rotateWorkspaceVaultZk.mockImplementation(async (_id, body) => ({ ...state, kv: body.new_kv }))

    await rotateWorkspaceVaultKey(VAULT, me as never)

    expect(api.getWorkspaceVaultZk).toHaveBeenCalledWith(VAULT, true)
    const [id, body] = api.rotateWorkspaceVaultZk.mock.calls[0]
    expect(id).toBe(VAULT)
    expect(body.new_kv).toBe(4)
    expect(body.wraps.map((w: { target: string }) => w.target).sort()).toEqual(['7', 'dev-1'])
    const mine = body.wraps.find((w: { target: string }) => w.target === ME)
    const vk = await zk.unwrapEcdhEs(mine.wrap, me.privateKey, me.publicJwk, zk.memberInfo(BINDING, 4, ME))
    const boxes = body.wraps.find((w: { target: string }) => w.target === 'dev-1')
    const boxVk = await zk.unwrapEcdhEs(boxes.wrap, box.privateKey, box.publicJwk, zk.deviceInfo(BINDING, 4, boxJkt))
    expect(Array.from(boxVk)).toEqual(Array.from(vk))
    const [value] = body.values
    expect(await zk.decryptValue(vk, BINDING, 'gog-token', 4, value.value_zk)).toBe('tok-value')
  })

  it('asks for my own recovery code first, and sends nothing without one', async () => {
    const { state, me } = await vaultAt(1)
    api.getKeyset.mockResolvedValue({ exists: true, seals: [{ alg: 'prf' }] })
    api.getWorkspaceVaultZk.mockResolvedValue(state)
    await expect(rotateWorkspaceVaultKey(VAULT, me as never)).rejects.toThrow(/recovery code/)
    expect(api.rotateWorkspaceVaultZk).not.toHaveBeenCalled()
  })
})
