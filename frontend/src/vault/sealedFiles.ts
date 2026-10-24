/**
 * A file in a private (sensitive) conversation, sealed to its people in this browser
 * (apps/canopy_sessions/private_attachments.py; docs/architecture/tenancy.md §
 * Sensitive conversations, "Files").
 *
 * The agent's copy is uploaded in clear for one turn and deleted once its box has
 * fetched it. The people's copy is made HERE, with the construction every message is
 * sealed with (ECDH-ES P-256 + HKDF-SHA256 + AES-256-GCM, `zk.ts::wrapEcdhEs`), to the
 * conversation's public key, bound to the conversation, the attachment and its
 * uploader:
 *
 *   name  `cf1:` + b64u(JSON {c, a, u, alg, epk, n, ct}), info and AAD
 *         `canopy-chat/v1/file-name|<c>|<a>|<u>`, plaintext JSON {name, type, size};
 *   bytes `cfb1` + uint32 BE header length + JSON {c, a, u, alg, epk, n} + raw ct, info
 *         and AAD `canopy-chat/v1/file-bytes|<c>|<a>|<u>`, plaintext = the file padded
 *         with zeros to `paddedLength` (so the stored size says little).
 *
 * canopy-web checks the shape and cannot open either; a member opens them with the
 * conversation key their vault key unwraps (`sealedChat.openConversationKey`).
 */
import type { ConversationKey } from './sealedChat'
import {
  b64u,
  concat,
  ecdhZ,
  gcmDecrypt,
  gcmEncrypt,
  hkdf,
  jwkToPoint,
  pointToJwk,
  publicJwk,
  randomBytes,
  unb64u,
  unwrapEcdhEs,
  wrapEcdhEs,
  type Bytes,
} from './zk'

export const NAME_PREFIX = 'cf1:'
const MAGIC = new Uint8Array([0x63, 0x66, 0x62, 0x31]) // "cfb1"
const enc = new TextEncoder()
const dec = new TextDecoder()

export const nameInfo = (c: string, a: string, u: string | number) => `canopy-chat/v1/file-name|${c}|${a}|${u}`
export const blobInfo = (c: string, a: string, u: string | number) => `canopy-chat/v1/file-bytes|${c}|${a}|${u}`

/** What the sealed name holds. */
export interface FileMeta { name: string; type: string; size: number }

/** Where a sealed file belongs: what its envelopes must name to open. */
export interface FilePlace { id: string; uploadedBy: string | number }

/** The target to seal to: the conversation and its public key (`SealedKey`). */
export interface SealTarget { conversation: string; public_jwk: { x?: string; y?: string } }

/** The size a file is padded to before sealing: a 4 KiB floor, then the next multiple of
 *  an eighth of the power of two below it — never more than an eighth bigger than the file. */
export function paddedLength(n: number): number {
  if (n <= 4096) return 4096
  const step = 2 ** Math.max(9, Math.floor(Math.log2(n)) - 3)
  return Math.ceil(n / step) * step
}

/** The people's copy of one file: its sealed name and its sealed bytes. */
export async function sealAttachment(target: SealTarget, place: FilePlace,
                                     file: { name: string; type: string; bytes: Uint8Array }):
    Promise<{ sealedName: string; blob: Bytes }> {
  const c = target.conversation
  const a = place.id
  const u = String(place.uploadedBy)
  const recipient = publicJwk(target.public_jwk)
  const meta: FileMeta = { name: file.name, type: file.type, size: file.bytes.length }
  const head = await wrapEcdhEs(enc.encode(JSON.stringify(meta)), recipient, nameInfo(c, a, u))
  const sealedName = NAME_PREFIX + b64u(enc.encode(JSON.stringify({ c, a, u, ...head })))

  const padded = new Uint8Array(paddedLength(file.bytes.length))
  padded.set(file.bytes)
  const eph = (await globalThis.crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true,
    ['deriveBits'])) as CryptoKeyPair
  const ephPub = publicJwk(await globalThis.crypto.subtle.exportKey('jwk', eph.publicKey))
  const epk = jwkToPoint(ephPub)
  const nonce = randomBytes(12)
  const info = blobInfo(c, a, u)
  const kek = await hkdf(await ecdhZ(eph.privateKey, recipient), concat(epk, jwkToPoint(recipient)), info)
  const ct = await gcmEncrypt(kek, nonce, padded, enc.encode(info))
  const header = enc.encode(JSON.stringify({ c, a, u, alg: 'ecdh-es', epk: b64u(epk), n: b64u(nonce) }))
  const len = new Uint8Array(4)
  new DataView(len.buffer).setUint32(0, header.length)
  return { sealedName, blob: concat(MAGIC, len, header, ct) }
}

function checkPlace(body: { c?: unknown; a?: unknown; u?: unknown }, key: ConversationKey, at: FilePlace) {
  if (body.c !== key.conversation) throw new Error('this file belongs to another conversation')
  if (body.a !== at.id) throw new Error('this file was sealed as another attachment')
  if (body.u !== String(at.uploadedBy)) throw new Error('this file was sealed by someone else')
}

/** Open a sealed name, at the attachment it was served as. Throws when it is not this
 *  conversation's, names another attachment or uploader, or was altered. */
export async function openSealedName(sealedName: string, key: ConversationKey, at: FilePlace): Promise<FileMeta> {
  if (!sealedName.startsWith(NAME_PREFIX)) throw new Error('not a sealed file name')
  const body = JSON.parse(dec.decode(unb64u(sealedName.slice(NAME_PREFIX.length)))) as
    { c: string; a: string; u: string; alg: 'ecdh-es'; epk: string; n: string; ct: string }
  checkPlace(body, key, at)
  const raw = await unwrapEcdhEs({ alg: body.alg, epk: body.epk, n: body.n, ct: body.ct },
    key.privateKey, key.publicJwk, nameInfo(body.c, body.a, body.u))
  const meta = JSON.parse(dec.decode(raw)) as Partial<FileMeta>
  return { name: String(meta.name ?? 'Attachment'), type: String(meta.type ?? 'application/octet-stream'),
           size: Number(meta.size ?? 0) }
}

/** Open sealed bytes, at the attachment they were served as; `meta` (from the name)
 *  says how much of the padded plaintext is the file. */
export async function openSealedBlob(blob: Uint8Array, key: ConversationKey, at: FilePlace,
                                     meta: FileMeta): Promise<Bytes> {
  if (blob.length < 8 || MAGIC.some((b, i) => blob[i] !== b)) throw new Error('not a sealed file')
  const n = new DataView(blob.buffer, blob.byteOffset, blob.byteLength).getUint32(4)
  const header = JSON.parse(dec.decode(blob.subarray(8, 8 + n))) as
    { c: string; a: string; u: string; alg: string; epk: string; n: string }
  checkPlace(header, key, at)
  if (header.alg !== 'ecdh-es') throw new Error('not an ecdh-es envelope')
  const epk = unb64u(header.epk)
  const info = blobInfo(header.c, header.a, header.u)
  const kek = await hkdf(await ecdhZ(key.privateKey, pointToJwk(epk)), concat(epk, jwkToPoint(key.publicJwk)), info)
  const plain = await gcmDecrypt(kek, unb64u(header.n), blob.subarray(8 + n), enc.encode(info))
  if (meta.size > plain.length) throw new Error('the file is shorter than its name says')
  return plain.slice(0, meta.size)
}
