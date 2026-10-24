// @vitest-environment jsdom
// A chat a browser starts carries a key pair made here, wrapped to the starter's vault
// key's PUBLIC half, so starting never asks to unlock. A key pinned here is used; one
// never seen here is trusted on first use — pinned as unconfirmed with the chat's id —
// and checked against the key the person actually holds the first time they unlock it
// here, which names the chats if it does not match. A CHANGED key stops the chat. Driven through chat creation
// itself (`createSession`, the chat page's; `offeredKey`, the panel's), real crypto,
// only fetch and the unlock mocked.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createSession } from '@/api/chat'
import { offeredKey } from '@/embed/offeredKey'
import { pinPerson, rememberSelf, selfMismatch, checkSelf } from './trust'
import { generateKeyPair, jkt, unwrapEcdhEs } from './zk'
import { wrapInfo } from './sealedChat'

let store: Map<string, string>
beforeEach(() => {
  store = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  })
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

type Pair = Awaited<ReturnType<typeof generateKeyPair>>

/** The server's answers: `named` is the vault key it says is mine (404 when null). */
function server(named: { pair: Pair; jkt: string } | null) {
  const created: unknown[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (String(url).endsWith('/my-vault-key')) {
      if (!named) return new Response(JSON.stringify({ detail: 'you have no vault key' }), { status: 404 })
      return new Response(JSON.stringify({ user_id: '7', public_jwk: named.pair.publicJwk, jkt: named.jkt }))
    }
    created.push(JSON.parse(String(init?.body ?? '{}')))
    return new Response(JSON.stringify({ id: 's1', workspace: 'w1' }))
  }))
  return created
}


describe('starting a chat from the chat page', () => {
  it('wraps to my pinned key', async () => {
    const mine = await generateKeyPair()
    pinPerson('7', await jkt(mine.publicJwk), '')
    const created = server({ pair: mine, jkt: await jkt(mine.publicJwk) })
    await createSession({ agentSlug: 'a' })
    const offer = (created[0] as { conversation_key: { wrap: never; conversation: string } }).conversation_key
    await expect(unwrapEcdhEs(offer.wrap, mine.privateKey, mine.publicJwk, wrapInfo(offer.conversation, '7')))
      .resolves.toBeTruthy()
  })

  it('stops, and starts nothing, when the key named as mine changed', async () => {
    const mine = await generateKeyPair()
    const substituted = await generateKeyPair()
    pinPerson('7', await jkt(mine.publicJwk), '')
    const created = server({ pair: substituted, jkt: await jkt(substituted.publicJwk) })
    await expect(createSession({ agentSlug: 'a' })).rejects.toThrow(/not the one this browser last opened/)
    expect(created).toEqual([])
  })

  it('stops on a key that does not match its thumbprint', async () => {
    const mine = await generateKeyPair()
    const created = server({ pair: mine, jkt: 'not-its-thumbprint' })
    await expect(createSession({ agentSlug: 'a' })).rejects.toThrow(/thumbprint/)
    expect(created).toEqual([])
  })

  it('with no pin, starts at once with a key wrapped to the named key, and pins it unconfirmed', async () => {
    const mine = await generateKeyPair()
    const thumb = await jkt(mine.publicJwk)
    const created = server({ pair: mine, jkt: thumb })
    await createSession({ agentSlug: 'a' })
    await createSession({ agentSlug: 'a' })
    const ids = created.map((c) => (c as { conversation_key: { conversation: string } }).conversation_key.conversation)
    expect(ids).toHaveLength(2)
    expect(JSON.parse(store.get('canopy-vault/v1/pins')!)['7']).toMatchObject({ jkt: thumb, unconfirmed: true, conversations: ids })
  })

  it('starts with no key for someone who has no vault key', async () => {
    const created = server(null)
    await createSession({ agentSlug: 'a' })
    expect((created[0] as { conversation_key?: unknown }).conversation_key).toBeUndefined()
  })
})

describe('the first unlock checks a key trusted on first use', () => {
  it('the key I hold matches: confirmed, no warning', async () => {
    const mine = await generateKeyPair()
    server({ pair: mine, jkt: await jkt(mine.publicJwk) })
    await createSession({ agentSlug: 'a' })
    await expect(rememberSelf('7', mine.publicJwk, mine.scalar)).resolves.toBeNull()
    expect(JSON.parse(store.get('canopy-vault/v1/pins')!)['7'].unconfirmed).toBeUndefined()
    expect(selfMismatch('7')).toBeNull()
  })

  it('a substituted key: names the chats wrapped to it, and is refused from then on', async () => {
    const mine = await generateKeyPair()
    const substituted = await generateKeyPair()
    const created = server({ pair: substituted, jkt: await jkt(substituted.publicJwk) })
    await createSession({ agentSlug: 'a' })
    const id = (created[0] as { conversation_key: { conversation: string } }).conversation_key.conversation
    const found = await rememberSelf('7', mine.publicJwk, mine.scalar)
    expect(found).toMatchObject({ wrongJkt: await jkt(substituted.publicJwk), conversations: [id] })
    expect(selfMismatch('7')).toMatchObject({ conversations: [id] })
    // The pin is now the key I hold: the substituted one stops the next chat.
    expect(checkSelf('7', await jkt(substituted.publicJwk))).toBe('changed')
    await expect(createSession({ agentSlug: 'a' })).rejects.toThrow(/not the one this browser last opened/)
  })
})

describe('starting a chat from the embedded panel', () => {
  const client = (named: { pair: Pair; jkt: string } | null) => ({
    rest: { json: vi.fn(async () => {
      if (!named) throw Object.assign(new Error('no vault key'), { status: 404 })
      return { user_id: '7', public_jwk: named.pair.publicJwk, jkt: named.jkt, keyset: null }
    }) },
  }) as never

  it('refuses a changed key', async () => {
    const mine = await generateKeyPair()
    const substituted = await generateKeyPair()
    pinPerson('7', await jkt(mine.publicJwk), '')
    await expect(offeredKey(client({ pair: substituted, jkt: await jkt(substituted.publicJwk) }), 'w1'))
      .rejects.toThrow(/not the one this browser last opened/)
  })

  it('with no pin, offers a key wrapped to the named key, without unlocking anything', async () => {
    const mine = await generateKeyPair()
    const got = await offeredKey(client({ pair: mine, jkt: await jkt(mine.publicJwk) }), 'w1')
    const offer = got.conversation_key as { wrap: never; conversation: string }
    await expect(unwrapEcdhEs(offer.wrap, mine.privateKey, mine.publicJwk, wrapInfo(offer.conversation, '7')))
      .resolves.toBeTruthy()
  })

  it('a visitor with no vault key starts with none', async () => {
    await expect(offeredKey(client(null), 'w1')).resolves.toEqual({})
  })
})
