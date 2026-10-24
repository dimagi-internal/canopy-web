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
 *   message `cc1:` + b64u(JSON {c, i, r?, alg, epk, n, ct}), info and AAD
 *           `canopy-chat/v1/message|<c>|<i>|<r>` (`|<r>` absent on an envelope sealed
 *           before the speaker was bound), plaintext = JSON {plaintext, content}.
 *           A row opens only where it says it belongs: its conversation, its index
 *           (`i == turn_index + d`, d in [0, …index_offsets], for a fork's copied
 *           rows) and its role.
 * `docs/vault/chat-v1/chat_v1_vectors.json` (made by the server) pins every byte;
 * `sealedChat.test.ts` opens it and reproduces the wrap.
 *
 * Nothing here sends a key anywhere: what leaves is a NEW wrap — for someone you add,
 * or of a conversation key this browser made for a chat it starts — which only that
 * person's vault key opens.
 */
import type { Unlocked } from './operations'
import { checkSelf } from './trust'
import {
  generateKeyPair,
  importPrivateScalar,
  jkt,
  publicJwkOfScalar,
  unb64u,
  unwrapEcdhEs,
  wrapEcdhEs,
  type EcdhWrap,
  type Ephemeral,
  type PublicJwk,
} from './zk'

export const SEALED_PREFIX = 'cc1:'
const dec = new TextDecoder()

export const messageInfo = (conversation: string, index: number, role?: string) =>
  `canopy-chat/v1/message|${conversation}|${index}${role ? `|${role}` : ''}`
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
  /** A fork's copied rows sit this far below the index their envelope names; null
   *  for a fork made before that was recorded (then only the conversation is checked). */
  index_offsets?: number[] | null
}

/** The conversation key, opened. Lives in this tab's memory only. */
export interface ConversationKey {
  conversation: string
  privateKey: CryptoKey
  publicJwk: Jwk
  /** Kept to wrap it again for someone you add. Never sent. */
  scalar: Uint8Array
  indexOffsets?: number[] | null
}

/** Where a stored row says it is: what its envelope must name to open. */
export interface RowPlace { index?: number; role?: string }

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
  return { conversation: key.conversation, privateKey: await importPrivateScalar(scalar, publicJwk), publicJwk, scalar,
           indexOffsets: key.index_offsets === undefined ? [] : key.index_offsets }
}

/** Open one stored `cc1:` envelope at `at` (the row it was served as). Throws when it is
 *  not this conversation's, names another index or speaker than the row, or was altered. */
export async function openSealed(sealed: string, key: ConversationKey, at: RowPlace = {}): Promise<Words> {
  if (!isSealed(sealed)) throw new Error('not a sealed message')
  const body = JSON.parse(dec.decode(unb64u(sealed.slice(SEALED_PREFIX.length)))) as
    EcdhWrap & { c: string; i: number; r?: string }
  if (body.c !== key.conversation) throw new Error('this message belongs to another conversation')
  if (at.index !== undefined && key.indexOffsets !== null) {
    const offsets = [0, ...(key.indexOffsets ?? [])]
    if (!offsets.some((d) => body.i === at.index! + d)) throw new Error('this message was sealed at another place')
  }
  if (body.r !== undefined && at.role !== undefined && body.r !== at.role) {
    throw new Error('this message was sealed as another speaker')
  }
  const raw = await unwrapEcdhEs({ alg: body.alg, epk: body.epk, n: body.n, ct: body.ct },
    key.privateKey, key.publicJwk, messageInfo(body.c, body.i, body.r))
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

/** `SessionCreateIn.conversation_key`: a key pair this browser makes for a chat it starts,
 *  its private half wrapped to the starter's own vault key. If the chat is, or becomes,
 *  private its words are sealed to it, and canopy-web never sees the private half. */
export interface ConversationKeyOffer {
  conversation: string
  public_jwk: PublicJwk
  wrap: EcdhWrap
  recipient_jkt: string
}

/** My vault key's public half, as `GET /my-vault-key` names it (with the keyset to open
 *  it, for the panel's frame, which has no browser session to read the vault with). */
export interface MyVaultKey {
  user_id: string
  public_jwk: Jwk | Record<string, unknown>
  jkt: string
  keyset?: SealedKey['keyset']
}

/** Why a new chat was not started: the vault key canopy-web named as yours is not one
 *  this browser can confirm is yours. Never answered by starting the chat anyway. */
export class VaultKeyRefused extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'VaultKeyRefused'
  }
}

export const CHANGED_SELF =
  'The vault key canopy names as yours is not the one this browser last opened, so this chat was ' +
  'not started. If you did not replace your vault key, someone may be trying to read your chats: ' +
  'tell your canopy admin. If you did, open it once (Settings → Vault key) and start again.'
export const UNCONFIRMED_SELF =
  'Open your vault key in this browser once to start a chat here: its key is made for you, and ' +
  'this browser must first see that the vault key canopy names is yours.'

/**
 * The key pair for a chat this browser starts, wrapped only to a vault key it has
 * CONFIRMED is the starter's (`newConversationKey`):
 *
 *  - pinned here before (`trust.checkSelf`) → used;
 *  - changed since → refused (`VaultKeyRefused`): that is what a substituted key looks like;
 *  - never seen here → `derive` opens the vault key (passkey or passphrase) and its public
 *    half is computed from the private scalar it opened — the server's word is never taken;
 *    a mismatch is refused, and so is no way to derive (`derive` absent or failing).
 *
 * Nothing falls back to a key the server makes.
 */
export async function offerForNewChat(me: MyVaultKey,
                                      derive?: () => Promise<Pick<Unlocked, 'scalar' | 'publicJwk'>>):
    Promise<ConversationKeyOffer> {
  const thumb = await jkt(me.public_jwk as Jwk)
  if (thumb !== me.jkt) throw new VaultKeyRefused('Your vault key does not match its thumbprint, so this chat was not started.')
  const seen = checkSelf(me.user_id, thumb)
  if (seen === 'changed') throw new VaultKeyRefused(CHANGED_SELF)
  if (seen === 'new') {
    if (!derive) throw new VaultKeyRefused(UNCONFIRMED_SELF)
    let opened: Pick<Unlocked, 'scalar' | 'publicJwk'>
    try {
      opened = await derive()
    } catch (e) {
      throw new VaultKeyRefused(`${UNCONFIRMED_SELF} (${e instanceof Error ? e.message : 'not opened'})`)
    }
    if (!opened.scalar) throw new VaultKeyRefused(UNCONFIRMED_SELF)
    const real = await jkt(await publicJwkOfScalar(opened.scalar))
    if (real !== thumb) throw new VaultKeyRefused(CHANGED_SELF)
  }
  return newConversationKey(me, { confirmed: true })
}

/**
 * A new conversation key, made here. Refuses (throws) when the vault key the server
 * names is not the one this browser knows is yours (`trust.checkSelf`): wrapping to a
 * substituted key would hand the conversation to whoever holds it.
 */
export async function newConversationKey(me: MyVaultKey,
                                         opts: { ephemeral?: Ephemeral; nonce?: Uint8Array; confirmed?: boolean } = {}):
    Promise<ConversationKeyOffer> {
  const mine = me.public_jwk as Jwk
  const thumb = await jkt(mine)
  if (thumb !== me.jkt) throw new VaultKeyRefused('your vault key does not match its thumbprint')
  const seen = checkSelf(me.user_id, thumb)
  if (seen === 'changed') throw new VaultKeyRefused(CHANGED_SELF)
  // A key never seen here is used only once it has been derived (`offerForNewChat`).
  if (seen === 'new' && !opts.confirmed) throw new VaultKeyRefused(UNCONFIRMED_SELF)
  const pair = await generateKeyPair()
  const conversation = globalThis.crypto.randomUUID()
  const wrap = await wrapEcdhEs(pair.scalar, mine as PublicJwk, wrapInfo(conversation, me.user_id), opts)
  pair.scalar.fill(0)
  return { conversation, public_jwk: pair.publicJwk, wrap, recipient_jkt: thumb }
}
