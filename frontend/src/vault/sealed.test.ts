// Server-minted values (`zks1`, sealed to recipients — canopy-web#1550 §6) in the
// browser flows: settling them under the vault key, writing before a vault key
// exists (kv 0), creating the first vault key from sealed values, and approving
// a device key + granting it in ONE passkey touch. The REAL crypto module runs;
// only the REST calls and the WebAuthn ceremony are mocked.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  getKeyset: vi.fn(), putKeyset: vi.fn(), getZkState: vi.fn(), enableZk: vi.fn(), addZkWraps: vi.fn(),
  putZkValues: vi.fn(), rotateZk: vi.fn(), disableZk: vi.fn(),
}))
vi.mock('@/api/vault', () => api)
const { revealAgentCredential } = vi.hoisted(() => ({ revealAgentCredential: vi.fn() }))
vi.mock('@/api/agents', () => ({ revealAgentCredential }))
const { approveRunnerDeviceKey } = vi.hoisted(() => ({ approveRunnerDeviceKey: vi.fn() }))
vi.mock('@/api/deviceKeys', () => ({ approveRunnerDeviceKey }))
const prf = vi.hoisted(() => ({ assertWithPrf: vi.fn() }))
vi.mock('@/vault/prf', async (orig) => ({ ...(await orig<typeof import('./prf')>()), ...prf }))

const zk = await import('./zk')
const ops = await import('./operations')

const USER = '7'
const AGENT_ID = '42'

async function principal() {
  const k = await zk.generateKeyPair()
  return { ...k, jkt: await zk.jkt(k.publicJwk) }
}

function unlocked(me: Awaited<ReturnType<typeof principal>>) {
  return async () => ({ userId: USER, privateKey: me.privateKey, publicJwk: me.publicJwk })
}

function baseState(over: Record<string, unknown> = {}) {
  return { agent: 'eva', agent_id: AGENT_ID, enabled: true, kv: 1, rotation_recommended: false, values: null,
           wraps: [], pending: [], sealed: [], ...over }
}

beforeEach(() => {
  api.putZkValues.mockResolvedValue({ kv: 1, names: [] })
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('settle', () => {
  it('re-encrypts a sealed value under the vault key, compare-and-swap on what it read', async () => {
    const me = await principal()
    const vk = zk.randomBytes(32)
    const stored = await zk.sealValue('minted-token', AGENT_ID, 'gog-token', 1,
      [{ kind: 'member', target: USER, publicJwk: me.publicJwk }])
    const myWrap = await zk.wrapEcdhEs(vk, me.publicJwk, zk.memberInfo(AGENT_ID, 1, USER))
    api.getZkState.mockResolvedValue(baseState({
      wraps: [{ kind: 'member', kv: 1, target: USER, current: true, wrap: myWrap }],
      values: { 'gog-token': stored, other: 'zk1:AAAA' }, sealed: ['gog-token'] }))
    await ops.grantPending('eva', unlocked(me))
    const { kv, values } = api.putZkValues.mock.calls[0][1]
    expect(kv).toBe(1)
    expect(values).toHaveLength(1)  // only the sealed one
    expect(values[0].expect).toBe(await zk.valueDigest(stored))
    expect(await zk.decryptValue(vk, AGENT_ID, 'gog-token', 1, values[0].value_zk)).toBe('minted-token')
    expect(api.addZkWraps).not.toHaveBeenCalled()  // nothing pending: settle only
  })

  it('skips a value sealed before I could read it, and never throws', async () => {
    const me = await principal(), device = await principal()
    const vk = zk.randomBytes(32)
    const stored = await zk.sealValue('x', AGENT_ID, 'gog-token', 1,
      [{ kind: 'device', target: device.jkt, publicJwk: device.publicJwk }])
    api.getZkState.mockResolvedValue(baseState({ values: { 'gog-token': stored }, sealed: ['gog-token'] }))
    expect(await ops.settleSealed('eva', vk, await unlocked(me)())).toBe(0)
    expect(api.putZkValues).not.toHaveBeenCalled()
  })
})

describe('born zero-knowledge (kv 0)', () => {
  it('a value saved before the vault key exists is sealed to every recipient', async () => {
    const me = await principal(), device = await principal()
    api.getZkState.mockResolvedValue(baseState({ kv: 0, pending: [
      { kind: 'device', target: 'dk1', public_jwk: device.publicJwk, ecdh_jkt: device.jkt },
      { kind: 'member', target: USER, public_jwk: me.publicJwk }] }))
    const unlock = vi.fn()
    await ops.setZkValue('eva', 'kindora', 'kin', unlock)
    expect(unlock).not.toHaveBeenCalled()  // nothing to unlock: sealing needs only public keys
    const { kv, values } = api.putZkValues.mock.calls[0][1]
    expect(kv).toBe(0)
    expect(await zk.openSealedValue(values[0].value_zk, AGENT_ID, 'kindora', 'device', device.jkt,
      device.privateKey)).toBe('kin')
    expect(await zk.openSealedValue(values[0].value_zk, AGENT_ID, 'kindora', 'member', USER,
      me.privateKey)).toBe('kin')
  })

  it('creating the vault key opens each sealed value with my keyset', async () => {
    const me = await principal()
    const stored = await zk.sealValue('kin', AGENT_ID, 'kindora', 0, [{ kind: 'member', target: USER, publicJwk: me.publicJwk }])
    api.getKeyset.mockResolvedValue({ user_id: USER, exists: true, public_jwk: me.publicJwk, seals: [], passkeys: [] })
    api.getZkState.mockResolvedValue(baseState({ kv: 0, values: { kindora: stored }, sealed: ['kindora'],
      pending: [{ kind: 'member', target: USER, public_jwk: me.publicJwk }] }))
    api.enableZk.mockResolvedValue(baseState())
    let code = ''
    await ops.turnOnZk('eva', ['kindora'], async (c) => void (code = c), unlocked(me))
    expect(revealAgentCredential).not.toHaveBeenCalled()
    const body = api.enableZk.mock.calls[0][1]
    const rec = body.wraps.find((w: { kind: string }) => w.kind === 'recovery').wrap
    const vk = await zk.unwrapRecovery(rec, AGENT_ID, 1, zk.parseRecoveryCode(code))
    expect(await zk.decryptValue(vk, AGENT_ID, 'kindora', 1, body.values[0].value_zk)).toBe('kin')
  })
})

describe('approve = wrap', () => {
  it('one passkey touch steps up, opens my keyset, approves, then wraps and settles', async () => {
    const me = await principal(), device = await principal()
    const vk = zk.randomBytes(32)
    const prfOut = zk.randomBytes(32)
    const seal = await zk.sealUserKey(me.scalar, USER, 'cred-1', prfOut)
    api.getKeyset.mockResolvedValue({ user_id: USER, exists: true, public_jwk: me.publicJwk, prf_input: 'cHJm',
                                      seals: [seal], passkeys: [] })
    prf.assertWithPrf.mockResolvedValue({ cred: 'cred-1', prf: prfOut })
    approveRunnerDeviceKey.mockResolvedValue({ id: 'dk1', status: 'active', zk_pending_agents: ['eva'] })
    const myWrap = await zk.wrapEcdhEs(vk, me.publicJwk, zk.memberInfo(AGENT_ID, 1, USER))
    const state = baseState({
      wraps: [{ kind: 'member', kv: 1, target: USER, current: true, wrap: myWrap }],
      pending: [{ kind: 'device', target: 'dk1', public_jwk: device.publicJwk, ecdh_jkt: device.jkt }] })
    api.getZkState.mockResolvedValue(state)
    api.addZkWraps.mockResolvedValue({ ...state, pending: [] })
    const prompt = vi.fn()
    const out = await ops.approveAndGrant('dk1', ['eva'], prompt)
    expect(prf.assertWithPrf).toHaveBeenCalledTimes(1)
    expect(prompt).not.toHaveBeenCalled()  // no second prompt, no passphrase
    expect(prf.assertWithPrf.mock.invocationCallOrder[0]).toBeLessThan(approveRunnerDeviceKey.mock.invocationCallOrder[0])
    expect(out).toMatchObject({ granted: ['eva'], failed: [] })
    const { wraps } = api.addZkWraps.mock.calls[0][1]
    expect(Array.from(await zk.unwrapEcdhEs(wraps[0].wrap, device.privateKey, device.publicJwk,
      zk.deviceInfo(AGENT_ID, 1, device.jkt)))).toEqual(Array.from(vk))
  })

  it('a key no zero-knowledge agent waits on is just approved', async () => {
    approveRunnerDeviceKey.mockResolvedValue({ id: 'dk1', status: 'active', zk_pending_agents: [] })
    const out = await ops.approveAndGrant('dk1', [], vi.fn())
    expect(out.granted).toEqual([])
    expect(prf.assertWithPrf).not.toHaveBeenCalled()
    expect(api.getKeyset).not.toHaveBeenCalled()
  })
})
