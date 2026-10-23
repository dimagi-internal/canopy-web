// What a stored envelope binds, as the browser checks it: the conversation, the index
// the row is served at (a fork's copied rows sit a recorded offset below theirs), and
// its speaker — against the vector the SERVER made (chat_v1_vectors.json). And the key
// pair a browser makes for a chat it starts.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import raw from '../../../docs/vault/chat-v1/chat_v1_vectors.json?raw'
import { messageInfo, newConversationKey, openConversationKey, openSealed, wrapInfo } from './sealedChat'
import { pinPerson } from './trust'
import { generateKeyPair, hexToBytes, importPrivateScalar, jkt, publicJwk, publicJwkOfScalar, unwrapEcdhEs } from './zk'

const V = JSON.parse(raw)
const I = V.inputs
const O = V.outputs

async function me() {
  const scalar = hexToBytes(I.user_priv_scalar_hex)
  const pub = await publicJwkOfScalar(scalar)
  return { userId: I.user_id, privateKey: await importPrivateScalar(scalar, pub), publicJwk: pub }
}
const sealedKey = (extra = {}) => ({ conversation: I.conversation, public_jwk: I.conversation_pub_jwk, wrap: O.wrap,
                                     current: true, ...extra })

beforeEach(() => {
  const store = new Map<string, string>()
  // This browser's pins (trust.ts), fresh for each test.
  vi.stubGlobal('window', { localStorage: {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  } })
})
afterEach(() => vi.unstubAllGlobals())

describe('an envelope opens only where it belongs', () => {
  it("binds its speaker: the server's role-bound envelope opens as that role and no other", async () => {
    expect(messageInfo(I.conversation, I.turn_index, 'assistant')).toBe(O.message_info_with_role)
    const key = await openConversationKey(sealedKey(), await me())
    expect(await openSealed(O.sealed_with_role, key, { index: I.turn_index, role: 'assistant' })).toEqual(I.words)
    await expect(openSealed(O.sealed_with_role, key, { index: I.turn_index, role: 'user' }))
      .rejects.toThrow(/another speaker/)
  })

  it('binds its index: served at another row it is refused', async () => {
    const key = await openConversationKey(sealedKey(), await me())
    expect(await openSealed(O.sealed, key, { index: I.turn_index })).toEqual(I.words)
    await expect(openSealed(O.sealed, key, { index: I.turn_index + 1 })).rejects.toThrow(/another place/)
  })

  it("a fork's copied row opens at its recorded offset, and only there", async () => {
    const fork = await openConversationKey(sealedKey({ index_offsets: [10] }), await me())
    expect(await openSealed(O.sealed, fork, { index: I.turn_index - 10 })).toEqual(I.words)
    await expect(openSealed(O.sealed, fork, { index: I.turn_index - 9 })).rejects.toThrow(/another place/)
    // A fork made before offsets were recorded: only the conversation is checked.
    const legacy = await openConversationKey(sealedKey({ index_offsets: null }), await me())
    expect(await openSealed(O.sealed, legacy, { index: 3 })).toEqual(I.words)
  })
})

describe('a chat started in this browser makes its own key', () => {
  it("wraps a fresh pair to my vault key: only my key opens it, to the pair's private half", async () => {
    const mine = await generateKeyPair()
    const thumb = await jkt(mine.publicJwk)
    const offer = await newConversationKey({ user_id: '7', public_jwk: mine.publicJwk, jkt: thumb })
    expect(offer.recipient_jkt).toBe(thumb)
    const scalar = await unwrapEcdhEs(offer.wrap, mine.privateKey, mine.publicJwk, wrapInfo(offer.conversation, '7'))
    expect(publicJwk(await publicJwkOfScalar(scalar))).toEqual(offer.public_jwk)
    const stranger = await generateKeyPair()
    await expect(unwrapEcdhEs(offer.wrap, stranger.privateKey, stranger.publicJwk,
      wrapInfo(offer.conversation, '7'))).rejects.toThrow()
  })

  it('refuses to wrap to a key that is not the one this browser last opened as mine', async () => {
    const mine = await generateKeyPair()
    const substituted = await generateKeyPair()
    pinPerson('7', await jkt(mine.publicJwk), '')
    await expect(newConversationKey({ user_id: '7', public_jwk: substituted.publicJwk,
                                      jkt: await jkt(substituted.publicJwk) })).rejects.toThrow(/last opened/)
    await expect(newConversationKey({ user_id: '7', public_jwk: mine.publicJwk, jkt: 'wrong' }))
      .rejects.toThrow(/thumbprint/)
    await expect(newConversationKey({ user_id: '7', public_jwk: mine.publicJwk, jkt: await jkt(mine.publicJwk) }))
      .resolves.toBeTruthy()
  })
})
