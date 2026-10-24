// @vitest-environment jsdom
// A chat a browser starts carries a key pair made here, wrapped to the starter's vault
// key — but only to a key this browser has CONFIRMED is theirs. A changed key stops the
// chat; a key never seen here is derived from the vault key the person opens now; and
// nothing ever falls back to a key canopy-web would make. Driven through chat creation
// itself (`createSession`, the chat page's; `offeredKey`, the panel's), real crypto,
// only fetch and the unlock mocked.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createSession } from '@/api/chat'
import { offeredKey } from '@/embed/offeredKey'
import { pinPerson } from './trust'
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

const opened = (pair: Pair) => async () => ({ scalar: pair.scalar, publicJwk: pair.publicJwk })

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
    await expect(createSession({ agentSlug: 'a', unlock: opened(mine) })).rejects.toThrow(/not the one this browser last opened/)
    expect(created).toEqual([])
  })

  it('with no pin, derives the key from the vault key I open, and refuses a substituted one', async () => {
    const mine = await generateKeyPair()
    const substituted = await generateKeyPair()
    let created = server({ pair: substituted, jkt: await jkt(substituted.publicJwk) })
    await expect(createSession({ agentSlug: 'a', unlock: opened(mine) })).rejects.toThrow(/not the one/)
    expect(created).toEqual([])
    // Without a way to open it, nothing starts either (never a server-made key).
    created = server({ pair: mine, jkt: await jkt(mine.publicJwk) })
    await expect(createSession({ agentSlug: 'a' })).rejects.toThrow(/Open your vault key/)
    expect(created).toEqual([])
    // Opened, and it is mine: the chat starts with a key wrapped to it.
    await createSession({ agentSlug: 'a', unlock: opened(mine) })
    expect((created[0] as { conversation_key?: unknown }).conversation_key).toBeTruthy()
  })

  it('starts with no key for someone who has no vault key', async () => {
    const created = server(null)
    await createSession({ agentSlug: 'a' })
    expect((created[0] as { conversation_key?: unknown }).conversation_key).toBeUndefined()
  })
})

describe('starting a chat from the embedded panel', () => {
  const client = (named: { pair: Pair; jkt: string } | null, keyset: unknown = null) => ({
    rest: { json: vi.fn(async () => {
      if (!named) throw Object.assign(new Error('no vault key'), { status: 404 })
      return { user_id: '7', public_jwk: named.pair.publicJwk, jkt: named.jkt, keyset }
    }) },
  }) as never

  it('refuses a changed key, and a key it cannot confirm without a keyset to open', async () => {
    const mine = await generateKeyPair()
    const substituted = await generateKeyPair()
    pinPerson('7', await jkt(mine.publicJwk), '')
    await expect(offeredKey(client({ pair: substituted, jkt: await jkt(substituted.publicJwk) }), 'w1', vi.fn()))
      .rejects.toThrow(/not the one this browser last opened/)
    store.clear()
    await expect(offeredKey(client({ pair: mine, jkt: await jkt(mine.publicJwk) }), 'w1', vi.fn()))
      .rejects.toThrow(/Open your vault key/)
  })

  it('a visitor with no vault key starts with none', async () => {
    await expect(offeredKey(client(null), 'w1', vi.fn())).resolves.toEqual({})
  })
})
