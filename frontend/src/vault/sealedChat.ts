/**
 * A sensitive conversation, read in the browser (spec
 * 2026-10-23-sensitive-conversations-sealed-to-their-people §2).
 *
 * canopy-web seals every stored message's words to the conversation's public key
 * and keeps the private key only WRAPPED to each of its people's vault keys
 * (`apps/canopy_sessions/conversation_keys.py`). It cannot read them back; this
 * file is where they are read: unwrap your copy of the conversation key with your
 * unlocked vault key, then open each message.
 *
 * Both steps are the vault's own `wrapEcdhEs` construction (./zk.ts):
 *   wrap    info = `canopy-chat/v1/wrap|<conversation>|<user id>`, plaintext = the
 *           conversation key's 32-byte P-256 scalar;
 *   message `cc1:` + b64u(JSON {c, i, alg, epk, n, ct}), info and AAD
 *           `canopy-chat/v1/message|<c>|<i>`, plaintext = JSON {plaintext, content}.
 * `docs/vault/chat-v1/chat_v1_vectors.json` (made by the server) pins every byte;
 * `sealedChat.test.ts` opens it and reproduces the wrap.
 *
 * Nothing here sends a key anywhere: the only thing that leaves is a NEW wrap, for
 * someone you add, which only their vault key opens.
 */
import type { Unlocked } from './operations'
import {
  importPrivateScalar,
  unb64u,
  unwrapEcdhEs,
  wrapEcdhEs,
  type EcdhWrap,
  type Ephemeral,
  type PublicJwk,
} from './zk'

export const SEALED_PREFIX = 'cc1:'
const dec = new TextDecoder()

export const messageInfo = (conversation: string, index: number) =>
  `canopy-chat/v1/message|${conversation}|${index}`
export const wrapInfo = (conversation: string, userId: string | number) =>
  `canopy-chat/v1/wrap|${conversation}|${userId}`

type Jwk = { x?: string; y?: string }

/** `SessionDetailOut.sealed_key` — what the server gives a reader in the conversation. */
export interface SealedKey {
  conversation: string
  public_jwk: Jwk | Record<string, unknown>
  wrap: EcdhWrap | null
  recipient_jkt?: string
  current?: boolean
  keyset?: { user_id: string; prf_input: string; exists?: boolean; public_jwk: Record<string, unknown>;
             seals: Record<string, unknown>[] } | null
  needs_migration?: boolean
}

/** The conversation key, opened. Lives in this tab's memory only. */
export interface ConversationKey {
  conversation: string
  privateKey: CryptoKey
  publicJwk: Jwk
  /** Kept to wrap it again for someone you add. Never sent. */
  scalar: Uint8Array
}

export interface Words {
  plaintext: string
  content: Record<string, unknown>
}

export const isSealed = (value: unknown): value is string =>
  typeof value === 'string' && value.startsWith(SEALED_PREFIX)

/** Why this reader cannot open the conversation yet, or null when they can try. */
export function whyNot(key: SealedKey | null | undefined): 'no-wrap' | 'stale-wrap' | null {
  if (!key) return null
  if (!key.wrap) return 'no-wrap'
  if (key.current === false) return 'stale-wrap'
  return null
}

/** Unwrap your copy of the conversation key with your unlocked vault key. */
export async function openConversationKey(key: SealedKey, me: Unlocked): Promise<ConversationKey> {
  if (!key.wrap) throw new Error('You have not been given a key to this conversation.')
  const scalar = await unwrapEcdhEs(key.wrap, me.privateKey, me.publicJwk, wrapInfo(key.conversation, me.userId))
  const publicJwk = key.public_jwk as Jwk
  return { conversation: key.conversation, privateKey: await importPrivateScalar(scalar, publicJwk), publicJwk, scalar }
}

/** Open one stored `cc1:` envelope. Throws when it is not this conversation's, or was altered. */
export async function openSealed(sealed: string, key: ConversationKey): Promise<Words> {
  if (!isSealed(sealed)) throw new Error('not a sealed message')
  const body = JSON.parse(dec.decode(unb64u(sealed.slice(SEALED_PREFIX.length)))) as
    EcdhWrap & { c: string; i: number }
  if (body.c !== key.conversation) throw new Error('this message belongs to another conversation')
  const raw = await unwrapEcdhEs({ alg: body.alg, epk: body.epk, n: body.n, ct: body.ct },
    key.privateKey, key.publicJwk, messageInfo(body.c, body.i))
  const words = JSON.parse(dec.decode(raw)) as Partial<Words>
  return { plaintext: String(words.plaintext ?? ''), content: (words.content ?? {}) as Record<string, unknown> }
}

/** What a row reads as once opened: the words in place of the metadata columns
 *  (a client's own `client_id` kept, so the optimistic echo still matches). */
export function withWords<T extends { plaintext: string; content: Record<string, unknown> }>(row: T, words: Words): T {
  const clientId = row.content?.client_id
  return { ...row, plaintext: words.plaintext,
           content: { ...words.content, ...(clientId !== undefined ? { client_id: clientId } : {}) } }
}

/** The conversation key wrapped to someone you are adding (`POST …/participants`). */
export async function wrapForRecipient(key: ConversationKey,
                                       recipient: { user_id: number | string; public_jwk: Jwk | Record<string, unknown>; jkt: string },
                                       opts: { ephemeral?: Ephemeral; nonce?: Uint8Array } = {}):
    Promise<EcdhWrap & { recipient_jkt: string }> {
  const wrap = await wrapEcdhEs(key.scalar, recipient.public_jwk as PublicJwk,
    wrapInfo(key.conversation, recipient.user_id), opts)
  return { ...wrap, recipient_jkt: recipient.jkt }
}
