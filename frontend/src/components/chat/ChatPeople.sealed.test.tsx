// @vitest-environment jsdom
// Adding someone to a SENSITIVE conversation: the server cannot give them its key, so
// the adder's browser opens its own copy and wraps it to the newcomer's vault key — and
// the panel unlocks a vault key with no request to canopy (it has no browser session).
// The REAL crypto runs; only the REST calls and WebAuthn are mocked.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  getSession: vi.fn(), participantRecipient: vi.fn(), addParticipant: vi.fn(),
  listParticipants: vi.fn(), removeParticipant: vi.fn(), giveKeyCopy: vi.fn(),
}))
vi.mock('@/api/chat', () => api)
vi.mock('@/vault/useVaultUnlock', () => ({ useVaultUnlock: () => ({ unlock: vi.fn(), element: null }) }))

const { addPerson, giveKey, STALE_OWN_COPY } = await import('./ChatPeople')
const trust = await import('@/vault/trust')
const { wrapInfo } = await import('@/vault/sealedChat')
const { unlockLocally } = await import('@/vault/localUnlock')
const zk = await import('@/vault/zk')

// This browser's pins (trust.ts), fresh for each test.
beforeEach(() => {
  const store = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  })
})

afterEach(() => {
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

/** A sensitive conversation I hold a current copy of, and `them` to add. */
async function world() {
  const me = await zk.generateKeyPair()
  const them = await zk.generateKeyPair()
  const conv = await zk.generateKeyPair()
  api.getSession.mockResolvedValue({ sensitive: true, sealed_key: {
    conversation: 'conv-1', public_jwk: conv.publicJwk, current: true,
    wrap: await zk.wrapEcdhEs(conv.scalar, me.publicJwk, wrapInfo('conv-1', '7')) } })
  api.participantRecipient.mockResolvedValue({ user_id: 9, email: 'peer@dimagi.com', public_jwk: them.publicJwk,
                                               jkt: await zk.jkt(them.publicJwk) })
  api.addParticipant.mockResolvedValue([])
  api.giveKeyCopy.mockResolvedValue([])
  const unlock = async () => ({ userId: '7', privateKey: me.privateKey, publicJwk: me.publicJwk })
  return { me, them, conv, unlock }
}

describe('addPerson', () => {
  it("wraps the conversation key to the newcomer's vault key, in this browser", async () => {
    const me = await zk.generateKeyPair()
    const them = await zk.generateKeyPair()
    const conv = await zk.generateKeyPair()
    api.getSession.mockResolvedValue({ sensitive: true, sealed_key: {
      conversation: 'conv-1', public_jwk: conv.publicJwk, current: true,
      wrap: await zk.wrapEcdhEs(conv.scalar, me.publicJwk, wrapInfo('conv-1', '7')) } })
    api.participantRecipient.mockResolvedValue({ user_id: 9, email: 'peer@dimagi.com', public_jwk: them.publicJwk,
                                                 jkt: await zk.jkt(them.publicJwk) })
    api.addParticipant.mockResolvedValue([])
    // Pinned before: no question asked.
    trust.pinPerson(9, await zk.jkt(them.publicJwk), 'peer@dimagi.com')
    await addPerson('s1', 'peer@dimagi.com', 'editor',
      async () => ({ userId: '7', privateKey: me.privateKey, publicJwk: me.publicJwk }))
    const [sid, email, role, wrap] = api.addParticipant.mock.calls[0]
    expect([sid, email, role]).toEqual(['s1', 'peer@dimagi.com', 'editor'])
    expect(wrap.recipient_jkt).toBe(await zk.jkt(them.publicJwk))
    // Only their vault key opens it, to the same conversation key.
    const opened = await zk.unwrapEcdhEs(wrap, them.privateKey, them.publicJwk, wrapInfo('conv-1', 9))
    expect(Array.from(opened)).toEqual(Array.from(conv.scalar))
    await expect(zk.unwrapEcdhEs(wrap, me.privateKey, me.publicJwk, wrapInfo('conv-1', 9))).rejects.toThrow()
  })

  it('never wraps to a key it has not been told to trust: the fingerprint is the question', async () => {
    const { them, unlock } = await world()
    const fp = zk.fingerprint(await zk.jkt(them.publicJwk))
    // The default refuses, saying which fingerprint to check.
    await expect(addPerson('s1', 'peer@dimagi.com', 'editor', unlock)).rejects.toThrow(fp)
    expect(api.addParticipant).not.toHaveBeenCalled()
    // Declined in the prompt: nothing shared.
    const no = vi.fn(async () => false)
    await expect(addPerson('s1', 'peer@dimagi.com', 'editor', unlock, undefined, no)).rejects.toThrow(/Not added/)
    expect(no).toHaveBeenCalledWith(expect.objectContaining({ email: 'peer@dimagi.com',
      verdict: expect.objectContaining({ status: 'new' }) }))
    expect(api.addParticipant).not.toHaveBeenCalled()
    // Accepted: added, and pinned, so the next add asks nothing.
    await addPerson('s1', 'peer@dimagi.com', 'editor', unlock, undefined, async () => true)
    expect(api.addParticipant).toHaveBeenCalledTimes(1)
    const asked = vi.fn(async () => true)
    await addPerson('s1', 'peer@dimagi.com', 'editor', unlock, undefined, asked)
    expect(asked).not.toHaveBeenCalled()
  })

  it('stops when a trusted person\'s key changed, or the key does not match its thumbprint', async () => {
    const { them, unlock } = await world()
    trust.pinPerson(9, 'an-older-thumbprint', 'peer@dimagi.com')
    await expect(addPerson('s1', 'peer@dimagi.com', 'editor', unlock)).rejects.toThrow(/changed/)
    const confirm = vi.fn(async (_who: { email: string; verdict: unknown }) => false)
    await expect(addPerson('s1', 'peer@dimagi.com', 'editor', unlock, undefined, confirm)).rejects.toThrow()
    expect(confirm.mock.calls[0]![0].verdict).toMatchObject({ status: 'changed', was: 'an-older-thumbprint' })
    api.participantRecipient.mockResolvedValue({ user_id: 9, email: 'peer@dimagi.com', public_jwk: them.publicJwk,
                                                 jkt: 'not-its-thumbprint' })
    await expect(addPerson('s1', 'peer@dimagi.com', 'editor', unlock, undefined, async () => true))
      .rejects.toThrow(/does not match its fingerprint/)
    expect(api.addParticipant).not.toHaveBeenCalled()
  })

  it('says what to do when your own copy is stale, instead of a crypto error', async () => {
    const { unlock } = await world()
    api.getSession.mockResolvedValue({ sensitive: true, sealed_key: {
      conversation: 'conv-1', public_jwk: {}, current: false, wrap: { alg: 'ecdh-es', epk: '', n: '', ct: '' } } })
    await expect(addPerson('s1', 'peer@dimagi.com', 'editor', unlock)).rejects.toThrow(STALE_OWN_COPY)
    // A copy marked current that still does not open says the same.
    const { conv } = await world()
    const other = await zk.generateKeyPair()
    api.getSession.mockResolvedValue({ sensitive: true, sealed_key: {
      conversation: 'conv-1', public_jwk: conv.publicJwk, current: true,
      wrap: await zk.wrapEcdhEs(conv.scalar, other.publicJwk, wrapInfo('conv-1', '7')) } })
    await expect(addPerson('s1', 'peer@dimagi.com', 'editor', unlock, undefined, async () => true))
      .rejects.toThrow(STALE_OWN_COPY)
  })

  it('gives someone already in it their copy, by the same rule', async () => {
    const { them, conv, unlock } = await world()
    await giveKey('s1', { user_id: 9, email: 'peer@dimagi.com' }, unlock, undefined, async () => true)
    const [sid, userId, wrap] = api.giveKeyCopy.mock.calls[0]
    expect([sid, userId]).toEqual(['s1', 9])
    const opened = await zk.unwrapEcdhEs(wrap, them.privateKey, them.publicJwk, wrapInfo('conv-1', 9))
    expect(Array.from(opened)).toEqual(Array.from(conv.scalar))
    await expect(giveKey('s1', { user_id: 10, email: 'peer@dimagi.com' }, unlock, undefined, async () => true))
      .rejects.toThrow(/not the person/)
  })

  it('adds to an ordinary conversation with no key and no unlock', async () => {
    api.getSession.mockResolvedValue({ sensitive: false, sealed_key: null })
    api.addParticipant.mockResolvedValue([])
    const unlock = vi.fn()
    await addPerson('s1', 'peer@dimagi.com', 'viewer', unlock)
    expect(api.addParticipant).toHaveBeenCalledWith('s1', 'peer@dimagi.com', 'viewer')
    expect(unlock).not.toHaveBeenCalled()
  })

  it('refuses when the adder holds no key to give', async () => {
    api.getSession.mockResolvedValue({ sensitive: true, sealed_key: { conversation: 'c', public_jwk: {}, wrap: null } })
    await expect(addPerson('s1', 'peer@dimagi.com', 'editor', vi.fn())).rejects.toThrow(/no key/)
    expect(api.addParticipant).not.toHaveBeenCalled()
  })
})

describe('unlockLocally (the panel)', () => {
  it('opens a passkey seal with the PRF output, asking canopy nothing', async () => {
    const person = await zk.generateKeyPair()
    const prf = zk.randomBytes(32)
    const seal = await zk.sealUserKey(person.scalar, '7', 'Y3JlZA', prf)
    const get = vi.fn(async () => ({ id: 'Y3JlZA', getClientExtensionResults: () => ({ prf: { results: { first: prf.buffer } } }) }))
    vi.stubGlobal('navigator', { credentials: { get } })
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    const me = await unlockLocally({ user_id: '7', prf_input: zk.b64u(zk.randomBytes(32)),
                                    public_jwk: person.publicJwk, seals: [seal as unknown as Record<string, unknown>] },
                                  async () => ({ method: 'passkey' }))
    expect(me.userId).toBe('7')
    expect(Array.from(me.scalar!)).toEqual(Array.from(person.scalar))
    expect(fetchSpy).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })

  it('one click goes straight to the passkey: no second "use my passkey" step', async () => {
    const person = await zk.generateKeyPair()
    const prf = zk.randomBytes(32)
    const seal = await zk.sealUserKey(person.scalar, '7', 'Y3JlZA', prf)
    const pass = await zk.sealUserKey2skd(person.scalar, '7', 'correct horse battery', zk.randomBytes(16))
    const get = vi.fn(async () => ({ id: 'Y3JlZA', getClientExtensionResults: () => ({ prf: { results: { first: prf.buffer } } }) }))
    vi.stubGlobal('navigator', { credentials: { get } })
    const prompt = vi.fn()
    const pending = unlockLocally({ user_id: '7', prf_input: zk.b64u(zk.randomBytes(32)), public_jwk: person.publicJwk,
                                    seals: [seal, pass] as unknown as Record<string, unknown>[] }, prompt)
    // Called synchronously from the click, inside its user gesture.
    expect(get).toHaveBeenCalledTimes(1)
    const me = await pending
    expect(Array.from(me.scalar!)).toEqual(Array.from(person.scalar))
    expect(prompt).not.toHaveBeenCalled()
    vi.unstubAllGlobals()
  })

  it('falls back to the passphrase, and only it, when the passkey fails here', async () => {
    const person = await zk.generateKeyPair()
    const sk = zk.randomBytes(16)
    const seal = await zk.sealUserKey(person.scalar, '7', 'Y3JlZA', zk.randomBytes(32))
    const pass = await zk.sealUserKey2skd(person.scalar, '7', 'correct horse battery', sk)
    vi.stubGlobal('navigator', { credentials: { get: vi.fn(async () => { throw new Error('NotAllowedError') }) } })
    const prompt = vi.fn(async () => ({ method: 'passphrase' as const, passphrase: 'correct horse battery',
                                        secretKey: zk.secretKeyCode(sk) }))
    const me = await unlockLocally({ user_id: '7', prf_input: zk.b64u(zk.randomBytes(32)), public_jwk: person.publicJwk,
                                     seals: [seal, pass] as unknown as Record<string, unknown>[] }, prompt)
    expect(Array.from(me.scalar!)).toEqual(Array.from(person.scalar))
    expect(prompt).toHaveBeenCalledWith(expect.objectContaining({ passkey: false, passphrase: true }))
    vi.unstubAllGlobals()
  }, 30_000)

  it('opens a passphrase seal', async () => {
    const person = await zk.generateKeyPair()
    const sk = zk.randomBytes(16)
    const seal = await zk.sealUserKey2skd(person.scalar, '7', 'correct horse battery', sk)
    const me = await unlockLocally({ user_id: '7', prf_input: '', public_jwk: person.publicJwk,
                                    seals: [seal as unknown as Record<string, unknown>] },
                                  async () => ({ method: 'passphrase', passphrase: 'correct horse battery',
                                                 secretKey: zk.secretKeyCode(sk) }))
    expect(Array.from(me.scalar!)).toEqual(Array.from(person.scalar))
    await expect(unlockLocally({ user_id: '7', prf_input: '', public_jwk: person.publicJwk,
                                 seals: [seal as unknown as Record<string, unknown>] },
                               async () => ({ method: 'passphrase', passphrase: 'wrong', secretKey: zk.secretKeyCode(sk) })))
      .rejects.toThrow(/do not open/)
  }, 30_000)
})
