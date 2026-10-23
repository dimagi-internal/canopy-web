// A sensitive conversation read in the browser, against the vector the SERVER made
// (docs/vault/chat-v1/chat_v1_vectors.json, `conversation_keys.vectors()`): the
// server's wrap and seal open here, and the browser's wrap for someone it adds is
// byte for byte the server's.
import { describe, expect, it } from 'vitest'
import raw from '../../../docs/vault/chat-v1/chat_v1_vectors.json?raw'
import {
  SEALED_PREFIX,
  messageInfo,
  openConversationKey,
  openSealed,
  whyNot,
  withWords,
  wrapForRecipient,
  wrapInfo,
  type ConversationKey,
} from './sealedChat'
import {
  b64u,
  generateKeyPair,
  hexToBytes,
  importPrivateScalar,
  publicJwk,
  unb64u,
  unwrapEcdhEs,
  wrapEcdhEs,
  type EcdhWrap,
  type Ephemeral,
} from './zk'

const V = JSON.parse(raw) as {
  inputs: {
    conversation: string; user_id: string; turn_index: number
    conversation_priv_scalar_hex: string; conversation_pub_jwk: { x: string; y: string }
    user_priv_scalar_hex: string; user_pub_jwk: { x: string; y: string }
    ephemeral_wrap_scalar_hex: string; nonce_wrap_hex: string
    words: { plaintext: string; content: Record<string, unknown> }
  }
  outputs: { wrap_info: string; message_info: string; user_jkt: string; wrap: EcdhWrap; sealed: string }
}
const I = V.inputs
const O = V.outputs

/** TEST ONLY: a key pair from a fixed scalar (WebCrypto derives the public half). */
async function keyFromScalar(hex: string): Promise<Ephemeral> {
  const prefix = hexToBytes('308141020100301306072a8648ce3d020106082a8648ce3d030107042730250201010420')
  const der = new Uint8Array(prefix.length + 32)
  der.set(prefix)
  der.set(hexToBytes(hex), prefix.length)
  const k = await crypto.subtle.importKey('pkcs8', der, { name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits'])
  const pub = publicJwk(await crypto.subtle.exportKey('jwk', k))
  return { privateKey: await importPrivateScalar(hexToBytes(hex), pub), publicJwk: pub }
}

async function me() {
  const k = await keyFromScalar(I.user_priv_scalar_hex)
  return { userId: I.user_id, privateKey: k.privateKey, publicJwk: k.publicJwk }
}

const sealedKey = { conversation: I.conversation, public_jwk: I.conversation_pub_jwk, wrap: O.wrap, current: true }

describe('chat v1 vectors (made by the server)', () => {
  it('names what it binds', () => {
    expect(wrapInfo(I.conversation, I.user_id)).toBe(O.wrap_info)
    expect(messageInfo(I.conversation, I.turn_index)).toBe(O.message_info)
  })

  it("unwraps the server's wrap to the conversation key with the person's vault key", async () => {
    const key = await openConversationKey(sealedKey, await me())
    expect(Array.from(key.scalar)).toEqual(Array.from(hexToBytes(I.conversation_priv_scalar_hex)))
  })

  it("opens the server's sealed message, and nothing altered or misplaced", async () => {
    const key = await openConversationKey(sealedKey, await me())
    expect(await openSealed(O.sealed, key)).toEqual(I.words)
    const body = JSON.parse(new TextDecoder().decode(unb64u(O.sealed.slice(SEALED_PREFIX.length))))
    const reseal = (b: object) => SEALED_PREFIX + b64u(new TextEncoder().encode(JSON.stringify(b)))
    await expect(openSealed(reseal({ ...body, i: body.i + 1 }), key)).rejects.toThrow()
    await expect(openSealed(reseal({ ...body, c: 'another' }), key)).rejects.toThrow(/another conversation/)
  })

  it("wraps for someone added exactly as the server wraps: byte for byte", async () => {
    const key = await openConversationKey(sealedKey, await me())
    const got = await wrapForRecipient(key, { user_id: I.user_id, public_jwk: I.user_pub_jwk, jkt: O.user_jkt },
      { ephemeral: await keyFromScalar(I.ephemeral_wrap_scalar_hex), nonce: hexToBytes(I.nonce_wrap_hex) })
    expect(got).toEqual({ ...O.wrap, recipient_jkt: O.user_jkt })
  })

  it('refuses without the right vault key or a wrap', async () => {
    const stranger = await generateKeyPair()
    await expect(openConversationKey(sealedKey,
      { userId: I.user_id, privateKey: stranger.privateKey, publicJwk: stranger.publicJwk })).rejects.toThrow()
    await expect(openConversationKey(sealedKey, { ...(await me()), userId: '8' })).rejects.toThrow()
    await expect(openConversationKey({ ...sealedKey, wrap: null }, await me())).rejects.toThrow(/not been given/)
    expect(whyNot({ ...sealedKey, wrap: null })).toBe('no-wrap')
    expect(whyNot({ ...sealedKey, current: false })).toBe('stale-wrap')
    expect(whyNot(sealedKey)).toBeNull()
  })
})

describe('reading a row', () => {
  it('replaces the metadata columns with the words, keeping the client id', () => {
    const row = { plaintext: '', content: { redacted: 'sensitive', client_id: 'c1' } }
    expect(withWords(row, { plaintext: 'hi', content: { text: 'hi' } }))
      .toEqual({ plaintext: 'hi', content: { text: 'hi', client_id: 'c1' } })
  })

  it('a re-wrap for someone added opens with their key to the same conversation key', async () => {
    const conv = await generateKeyPair()
    const key: ConversationKey = { conversation: 'c', privateKey: conv.privateKey, publicJwk: conv.publicJwk,
                                   scalar: conv.scalar }
    const them = await generateKeyPair()
    const wrap = await wrapForRecipient(key, { user_id: 9, public_jwk: them.publicJwk, jkt: 'j' })
    const opened = await unwrapEcdhEs(wrap, them.privateKey, them.publicJwk, wrapInfo('c', 9))
    expect(Array.from(opened)).toEqual(Array.from(conv.scalar))
    // And the envelope a server would write opens with it.
    const env = await wrapEcdhEs(new TextEncoder().encode(JSON.stringify({ plaintext: 'x', content: {} })),
      conv.publicJwk, messageInfo('c', 4))
    const sealed = SEALED_PREFIX + b64u(new TextEncoder().encode(JSON.stringify({ c: 'c', i: 4, ...env })))
    expect((await openSealed(sealed, key)).plaintext).toBe('x')
  })
})
