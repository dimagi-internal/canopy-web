/**
 * Canopy Vault zero-knowledge v1 — the browser's half (canopy-web#1550).
 *
 * The NORMATIVE definition of every byte is `docs/vault/zk-v1/zk_v1_reference.py`;
 * `zk.test.ts` reproduces every output of `zk_v1_vectors.json` byte for byte.
 * WebCrypto only: ECDH P-256 (deriveBits → the 32-byte x-coordinate Z),
 * HKDF-SHA256 (L = 32), AES-256-GCM (12-byte nonce, 16-byte tag appended).
 * Encoding: base64url without padding. Points: X9.62 uncompressed (65 bytes).
 *
 * The randomness every operation needs (ephemeral keys, nonces, salts) is drawn
 * here; `opts` exists ONLY so the vector test can pin it — production callers
 * never pass it.
 *
 * Nothing in this file talks to the server, and no secret it produces may be
 * sent to one: canopy-web stores only what these functions call ciphertext,
 * wraps and seals.
 */

export const ZK_PREFIX = 'zk1:'
const enc = new TextEncoder()
const dec = new TextDecoder()
const subtle = () => globalThis.crypto.subtle

export type Bytes = Uint8Array<ArrayBuffer>
export interface EcdhWrap { alg: 'ecdh-es'; epk: string; n: string; ct: string }
export interface RecoveryWrap { alg: 'recovery'; s: string; n: string; ct: string }
export interface UserKeySeal { alg: 'prf'; cred: string; s: string; n: string; ct: string }
export interface PublicJwk { kty: 'EC'; crv: 'P-256'; x: string; y: string }
/** An injected ephemeral key — the vector test's only. */
export interface Ephemeral { privateKey: CryptoKey; publicJwk: PublicJwk }

// ---- encoding -------------------------------------------------------------------

export function b64u(bytes: Uint8Array): string {
  let bin = ''
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i])
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

export function unb64u(value: string): Bytes {
  if (!/^[A-Za-z0-9_-]*$/.test(value)) throw new Error('not base64url')
  const b64 = value.replace(/-/g, '+').replace(/_/g, '/')
  const bin = atob(b64 + '='.repeat((4 - (b64.length % 4)) % 4))
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

export function hexToBytes(hex: string): Bytes {
  const out = new Uint8Array(hex.length / 2)
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(2 * i, 2 * i + 2), 16)
  return out
}

export function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

function concat(...parts: Uint8Array[]): Bytes {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0))
  let at = 0
  for (const p of parts) {
    out.set(p, at)
    at += p.length
  }
  return out
}

export function randomBytes(n: number): Bytes {
  return globalThis.crypto.getRandomValues(new Uint8Array(n))
}

// ---- primitives -----------------------------------------------------------------

export async function hkdf(ikm: Uint8Array, salt: Uint8Array, info: string): Promise<Bytes> {
  const key = await subtle().importKey('raw', ikm as Bytes, 'HKDF', false, ['deriveBits'])
  const bits = await subtle().deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt: salt as Bytes, info: enc.encode(info) }, key, 256)
  return new Uint8Array(bits)
}

async function aesKey(raw: Uint8Array, usage: KeyUsage): Promise<CryptoKey> {
  return subtle().importKey('raw', raw as Bytes, { name: 'AES-GCM' }, false, [usage])
}

export async function gcmEncrypt(key: Uint8Array, nonce: Uint8Array, plaintext: Uint8Array,
                                 aad: Uint8Array): Promise<Bytes> {
  const ct = await subtle().encrypt({ name: 'AES-GCM', iv: nonce as Bytes, additionalData: aad as Bytes },
    await aesKey(key, 'encrypt'), plaintext as Bytes)
  return new Uint8Array(ct)
}

export async function gcmDecrypt(key: Uint8Array, nonce: Uint8Array, ciphertext: Uint8Array,
                                 aad: Uint8Array): Promise<Bytes> {
  const pt = await subtle().decrypt({ name: 'AES-GCM', iv: nonce as Bytes, additionalData: aad as Bytes },
    await aesKey(key, 'decrypt'), ciphertext as Bytes)
  return new Uint8Array(pt)
}

export async function sha256(data: Uint8Array): Promise<Bytes> {
  return new Uint8Array(await subtle().digest('SHA-256', data as Bytes))
}

// ---- P-256 keys -------------------------------------------------------------------

export function jwkToPoint(jwk: { x?: string; y?: string }): Bytes {
  const x = unb64u(jwk.x ?? ''), y = unb64u(jwk.y ?? '')
  if (x.length !== 32 || y.length !== 32) throw new Error('not a P-256 public key')
  return concat(Uint8Array.of(4), x, y)
}

export function pointToJwk(point: Uint8Array): PublicJwk {
  if (point.length !== 65 || point[0] !== 4) throw new Error('not an uncompressed P-256 point')
  return { kty: 'EC', crv: 'P-256', x: b64u(point.slice(1, 33)), y: b64u(point.slice(33)) }
}

export function publicJwk(jwk: { x?: string; y?: string }): PublicJwk {
  return { kty: 'EC', crv: 'P-256', x: jwk.x ?? '', y: jwk.y ?? '' }
}

const ECDH = { name: 'ECDH', namedCurve: 'P-256' } as const

export async function importPublic(jwk: { x?: string; y?: string }): Promise<CryptoKey> {
  return subtle().importKey('jwk', publicJwk(jwk), ECDH, true, [])
}

/** A private key from its 32-byte big-endian scalar `d` and its public half. */
export async function importPrivateScalar(scalar: Uint8Array, pub: { x?: string; y?: string }): Promise<CryptoKey> {
  if (scalar.length !== 32) throw new Error('a P-256 scalar is 32 bytes')
  return subtle().importKey('jwk', { ...publicJwk(pub), d: b64u(scalar) }, ECDH, false, ['deriveBits'])
}

/** A fresh key pair whose scalar can be sealed (the person's keyset). */
export async function generateKeyPair(): Promise<{ privateKey: CryptoKey; publicJwk: PublicJwk; scalar: Bytes }> {
  const pair = (await subtle().generateKey(ECDH, true, ['deriveBits'])) as CryptoKeyPair
  const jwk = await subtle().exportKey('jwk', pair.privateKey)
  const scalar = unb64u(jwk.d ?? '')
  const pub = publicJwk(jwk)
  return { privateKey: await importPrivateScalar(scalar, pub), publicJwk: pub, scalar }
}

async function ecdhZ(priv: CryptoKey, pub: { x?: string; y?: string }): Promise<Bytes> {
  return new Uint8Array(await subtle().deriveBits({ name: 'ECDH', public: await importPublic(pub) }, priv, 256))
}

/** RFC 7638 thumbprint of an EC public JWK (as canopy-web computes `jkt`). */
export async function jkt(jwk: { x?: string; y?: string }): Promise<string> {
  const j = publicJwk(jwk)
  const canon = `{"crv":"${j.crv}","kty":"${j.kty}","x":"${j.x}","y":"${j.y}"}`
  return b64u(await sha256(enc.encode(canon)))
}

// ---- 2. values ---------------------------------------------------------------------

export function valueAad(agentId: string, name: string, kv: number): Bytes {
  return enc.encode(`canopy-vault/v1/value|${agentId}|${name}|${kv}`)
}

export async function encryptValue(vk: Uint8Array, agentId: string, name: string, kv: number,
                                   plaintext: string, opts: { nonce?: Uint8Array } = {}): Promise<string> {
  const nonce = opts.nonce ?? randomBytes(12)
  const ct = await gcmEncrypt(vk, nonce, enc.encode(plaintext), valueAad(agentId, name, kv))
  return ZK_PREFIX + b64u(concat(nonce, ct))
}

export async function decryptValue(vk: Uint8Array, agentId: string, name: string, kv: number,
                                   stored: string): Promise<string> {
  if (!stored.startsWith(ZK_PREFIX)) throw new Error('not a zk1 value')
  const blob = unb64u(stored.slice(ZK_PREFIX.length))
  return dec.decode(await gcmDecrypt(vk, blob.slice(0, 12), blob.slice(12), valueAad(agentId, name, kv)))
}

// ---- 6. sealed to recipients (zks1) ------------------------------------------------------
//
// What canopy-web mints itself (a Gmail token, a rotated canopy-pat) — or what
// anyone writes before an agent born zero-knowledge has a vault key (kv 0) — is
// sealed straight to each recipient's public key. A vault-key holder later
// "settles" it into an ordinary zk1 value.

export const SEALED_PREFIX = 'zks1:'
export interface SealedEntry { kind: 'device' | 'member'; target: string; epk: PublicJwk; ct: string }
export interface Recipient { kind: 'device' | 'member'; target: string; publicJwk: { x?: string; y?: string } }

export const isSealed = (stored: string) => stored.startsWith(SEALED_PREFIX)

export const sealInfo = (agentId: string, name: string, kv: number, kind: string, target: string) =>
  `canopy-vault/v1/seal-value|${agentId}|${name}|${kv}|${kind}:${target}`

export async function sealValue(plaintext: string, agentId: string, name: string, kv: number,
                                recipients: Recipient[],
                                opts: { ephemerals?: Ephemeral[]; nonces?: Uint8Array[] } = {}): Promise<string> {
  if (!recipients.length) throw new Error('Nobody can read this agent yet: approve a device or set up your vault key.')
  const r: SealedEntry[] = []
  for (let i = 0; i < recipients.length; i++) {
    const { kind, target, publicJwk: q } = recipients[i]
    let eph = opts.ephemerals?.[i]
    if (!eph) {
      const pair = (await subtle().generateKey(ECDH, true, ['deriveBits'])) as CryptoKeyPair
      eph = { privateKey: pair.privateKey, publicJwk: publicJwk(await subtle().exportKey('jwk', pair.publicKey)) }
    }
    const nonce = opts.nonces?.[i] ?? randomBytes(12)
    const key = await hkdf(await ecdhZ(eph.privateKey, q), new Uint8Array(0), sealInfo(agentId, name, kv, kind, target))
    const ct = await gcmEncrypt(key, nonce, enc.encode(plaintext), valueAad(agentId, name, kv))
    r.push({ kind, target, epk: publicJwk(eph.publicJwk), ct: b64u(concat(nonce, ct)) })
  }
  return SEALED_PREFIX + b64u(enc.encode(JSON.stringify({ kv, r })))
}

/** Open the entry sealed to (kind, target) with that recipient's private key. */
export async function openSealedValue(stored: string, agentId: string, name: string, kind: string, target: string,
                                      priv: CryptoKey): Promise<string> {
  if (!isSealed(stored)) throw new Error('not a zks1 value')
  const body = JSON.parse(dec.decode(unb64u(stored.slice(SEALED_PREFIX.length)))) as { kv: number; r: SealedEntry[] }
  const entry = body.r.find((e) => e.kind === kind && String(e.target) === target)
  if (!entry) throw new Error(`${name} was sealed before you could read it — a device holding it can settle it.`)
  const key = await hkdf(await ecdhZ(priv, entry.epk), new Uint8Array(0), sealInfo(agentId, name, body.kv, kind, target))
  const blob = unb64u(entry.ct)
  return dec.decode(await gcmDecrypt(key, blob.slice(0, 12), blob.slice(12), valueAad(agentId, name, body.kv)))
}

/** The compare-and-swap digest PUT /values `expect` names: hex SHA-256. */
export async function valueDigest(stored: string): Promise<string> {
  return stored ? bytesToHex(await sha256(enc.encode(stored))) : ''
}

// ---- 7. endorsements --------------------------------------------------------------------
//
// A person's signature, by their own vault key, that a recipient's key may read
// an agent — the publicly checkable twin of a wrap. A writer with no vault key
// seals a zks1 value only to recipients endorsed by a key it already trusts.
// The person key is the §4 ECDH scalar, imported as ECDSA for this one use; the
// fixed prefix keeps every signed message out of any ECDH derivation.

export interface Endorsement { kind: 'device' | 'member'; target: string; key_jkt: string; by_jkt: string; sig: string }

export const endorseMsg = (agentId: string, kind: string, target: string, keyJkt: string) =>
  `canopy-vault/v1/endorse|${agentId}|${kind}|${target}|${keyJkt}`

const ECDSA = { name: 'ECDSA', namedCurve: 'P-256' } as const
const ECDSA_SHA256 = { name: 'ECDSA', hash: 'SHA-256' } as const

/** Sign "this key may read this agent" with the person's scalar. */
export async function endorse(scalar: Uint8Array, signerPub: { x?: string; y?: string }, agentId: string,
                              kind: 'device' | 'member', target: string,
                              recipientPub: { x?: string; y?: string }): Promise<Endorsement> {
  if (scalar.length !== 32) throw new Error('a P-256 scalar is 32 bytes')
  const key = await subtle().importKey('jwk', { ...publicJwk(signerPub), d: b64u(scalar) }, ECDSA, false, ['sign'])
  const keyJkt = await jkt(recipientPub)
  const sig = new Uint8Array(await subtle().sign(ECDSA_SHA256, key, enc.encode(endorseMsg(agentId, kind, target, keyJkt))))
  return { kind, target, key_jkt: keyJkt, by_jkt: await jkt(signerPub), sig: b64u(sig) }
}

/** Is `e` the endorser's valid §7 signature for this agent? Never throws. */
export async function verifyEndorsement(e: { kind: string; target: string; key_jkt: string; by_jkt: string; sig: string },
                                        agentId: string, endorserPub: { x?: string; y?: string }): Promise<boolean> {
  try {
    if (e.by_jkt !== (await jkt(endorserPub))) return false
    const sig = unb64u(e.sig)
    if (sig.length !== 64) return false
    const key = await subtle().importKey('jwk', publicJwk(endorserPub), ECDSA, false, ['verify'])
    return await subtle().verify(ECDSA_SHA256, key, sig, enc.encode(endorseMsg(agentId, e.kind, e.target, e.key_jkt)))
  } catch {
    return false
  }
}

/** A thumbprint as a person compares it: groups of four, upper case, 8 groups. */
export function fingerprint(thumbprint: string): string {
  return (thumbprint.replace(/[^A-Za-z0-9]/g, '').toUpperCase().slice(0, 32).match(/.{1,4}/g) ?? []).join(' ')
}

// ---- 3. ECDH-ES wraps -------------------------------------------------------------------

export const deviceInfo = (agentId: string, kv: number, deviceJkt: string) =>
  `canopy-vault/v1/wrap-device|${agentId}|${kv}|${deviceJkt}`
export const memberInfo = (agentId: string, kv: number, userId: string) =>
  `canopy-vault/v1/wrap-member|${agentId}|${kv}|${userId}`

export async function wrapEcdhEs(vk: Uint8Array, recipient: { x?: string; y?: string }, info: string,
                                 opts: { ephemeral?: Ephemeral; nonce?: Uint8Array } = {}): Promise<EcdhWrap> {
  let eph = opts.ephemeral
  if (!eph) {
    const pair = (await subtle().generateKey(ECDH, true, ['deriveBits'])) as CryptoKeyPair
    eph = { privateKey: pair.privateKey, publicJwk: publicJwk(await subtle().exportKey('jwk', pair.publicKey)) }
  }
  const nonce = opts.nonce ?? randomBytes(12)
  const z = await ecdhZ(eph.privateKey, recipient)
  const epk = jwkToPoint(eph.publicJwk)
  const kek = await hkdf(z, concat(epk, jwkToPoint(recipient)), info)
  const ct = await gcmEncrypt(kek, nonce, vk, enc.encode(info))
  return { alg: 'ecdh-es', epk: b64u(epk), n: b64u(nonce), ct: b64u(ct) }
}

export async function unwrapEcdhEs(wrap: EcdhWrap, recipientPriv: CryptoKey,
                                   recipientPub: { x?: string; y?: string }, info: string): Promise<Bytes> {
  if (wrap.alg !== 'ecdh-es') throw new Error('not an ecdh-es wrap')
  const epk = unb64u(wrap.epk)
  const z = await ecdhZ(recipientPriv, pointToJwk(epk))
  const kek = await hkdf(z, concat(epk, jwkToPoint(recipientPub)), info)
  return gcmDecrypt(kek, unb64u(wrap.n), unb64u(wrap.ct), enc.encode(info))
}

// ---- 4. user keyset seal -------------------------------------------------------------------

export async function prfInput(userId: string): Promise<Bytes> {
  return sha256(enc.encode(`canopy-vault/v1/prf|${userId}`))
}

export const userKeyInfo = (userId: string, cred: string) => `canopy-vault/v1/user-key|${userId}|${cred}`

export async function sealUserKey(scalar: Uint8Array, userId: string, cred: string, prfOut: Uint8Array,
                                  opts: { salt?: Uint8Array; nonce?: Uint8Array } = {}): Promise<UserKeySeal> {
  const salt = opts.salt ?? randomBytes(16)
  const nonce = opts.nonce ?? randomBytes(12)
  const info = userKeyInfo(userId, cred)
  const kek = await hkdf(prfOut, salt, info)
  const ct = await gcmEncrypt(kek, nonce, scalar, enc.encode(info))
  return { alg: 'prf', cred, s: b64u(salt), n: b64u(nonce), ct: b64u(ct) }
}

export async function openUserKey(seal: UserKeySeal, userId: string, prfOut: Uint8Array): Promise<Bytes> {
  const info = userKeyInfo(userId, seal.cred)
  const kek = await hkdf(prfOut, unb64u(seal.s), info)
  return gcmDecrypt(kek, unb64u(seal.n), unb64u(seal.ct), enc.encode(info))
}

// ---- 4c. the person's recovery code -----------------------------------------------------------
// ONE code per person: it opens their key, and so every agent they hold.

export const userRecoveryInfo = (userId: string) => `canopy-vault/v1/user-key|${userId}|recovery`

export async function sealUserKeyRecovery(scalar: Uint8Array, userId: string, r: Uint8Array,
                                          opts: { salt?: Uint8Array; nonce?: Uint8Array } = {}): Promise<PersonRecoverySeal> {
  const salt = opts.salt ?? randomBytes(16)
  const nonce = opts.nonce ?? randomBytes(12)
  const info = userRecoveryInfo(userId)
  const kek = await hkdf(r, salt, info)
  const ct = await gcmEncrypt(kek, nonce, scalar, enc.encode(info))
  return { alg: 'recovery', s: b64u(salt), n: b64u(nonce), ct: b64u(ct) }
}

export async function openUserKeyRecovery(seal: PersonRecoverySeal, userId: string, r: Uint8Array): Promise<Bytes> {
  const info = userRecoveryInfo(userId)
  const kek = await hkdf(r, unb64u(seal.s), info)
  return gcmDecrypt(kek, unb64u(seal.n), unb64u(seal.ct), enc.encode(info))
}

// ---- 5. recovery ------------------------------------------------------------------------------

export const recoveryInfo = (agentId: string, kv: number) => `canopy-vault/v1/wrap-recovery|${agentId}|${kv}`

export async function wrapRecovery(vk: Uint8Array, agentId: string, kv: number, r: Uint8Array,
                                   opts: { salt?: Uint8Array; nonce?: Uint8Array } = {}): Promise<RecoveryWrap> {
  const salt = opts.salt ?? randomBytes(16)
  const nonce = opts.nonce ?? randomBytes(12)
  const info = recoveryInfo(agentId, kv)
  const kek = await hkdf(r, salt, info)
  const ct = await gcmEncrypt(kek, nonce, vk, enc.encode(info))
  return { alg: 'recovery', s: b64u(salt), n: b64u(nonce), ct: b64u(ct) }
}

export async function unwrapRecovery(wrap: RecoveryWrap, agentId: string, kv: number, r: Uint8Array): Promise<Bytes> {
  const info = recoveryInfo(agentId, kv)
  const kek = await hkdf(r, unb64u(wrap.s), info)
  return gcmDecrypt(kek, unb64u(wrap.n), unb64u(wrap.ct), enc.encode(info))
}

const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'

/** 256 bits → 52 Crockford chars (the last carries 1 bit, low bits zero), groups of 4. */
export function recoveryCode(r: Uint8Array): string {
  const n = r.length * 8
  let v = BigInt('0x' + (bytesToHex(r) || '0'))
  v <<= BigInt((5 - (n % 5)) % 5)
  const chars = Math.floor((n + 4) / 5)
  let s = ''
  for (let i = chars - 1; i >= 0; i--) s += CROCKFORD[Number((v >> BigInt(5 * i)) & 31n)]
  return s.match(/.{1,4}/g)!.join('-')
}

/** The code as typed: dashes/spaces ignored, case-insensitive, O→0, I/L→1. */
export function parseRecoveryCode(code: string): Bytes {
  const s = code.replace(/[-\s]/g, '').toUpperCase().replace(/O/g, '0').replace(/[IL]/g, '1')
  if (s.length !== 52) throw new Error('A recovery code is 52 characters (13 groups of 4)')
  let v = 0n
  for (const ch of s) {
    const i = CROCKFORD.indexOf(ch)
    if (i < 0) throw new Error(`"${ch}" is not in a recovery code`)
    v = (v << 5n) | BigInt(i)
  }
  v >>= BigInt(5 * s.length - 256)
  return hexToBytes(v.toString(16).padStart(64, '0'))
}

// ---- 4b. passphrase + Secret Key seal (2SKD) ------------------------------------------------

export const PBKDF2_ITERATIONS = 650000
export interface PassphraseSeal { alg: '2skd'; kdf: 'pbkdf2-sha256'; i: number; s: string; n: string; ct: string }
export interface PersonRecoverySeal { alg: 'recovery'; s: string; n: string; ct: string }
export type AnySeal = UserKeySeal | PassphraseSeal | PersonRecoverySeal

export const passphraseInfo = (userId: string) => `canopy-vault/v1/user-key|${userId}|passphrase`

async function twoSecretKek(passphrase: string, secretKey: Uint8Array, salt: Uint8Array, userId: string): Promise<Bytes> {
  if (secretKey.length !== 16) throw new Error('A Secret Key is 16 bytes')
  const pw = await subtle().importKey('raw', enc.encode(passphrase.normalize('NFKC')), 'PBKDF2', false, ['deriveBits'])
  const k1 = new Uint8Array(await subtle().deriveBits(
    { name: 'PBKDF2', hash: 'SHA-256', salt: salt as Bytes, iterations: PBKDF2_ITERATIONS }, pw, 256))
  const k2 = await hkdf(secretKey, salt, `canopy-vault/v1/secret-key|${userId}`)
  const mixed = k1.map((b, i) => b ^ k2[i])
  return hkdf(mixed, salt, passphraseInfo(userId))
}

export async function sealUserKey2skd(scalar: Uint8Array, userId: string, passphrase: string, secretKey: Uint8Array,
                                      opts: { salt?: Uint8Array; nonce?: Uint8Array } = {}): Promise<PassphraseSeal> {
  const salt = opts.salt ?? randomBytes(16)
  const nonce = opts.nonce ?? randomBytes(12)
  const kek = await twoSecretKek(passphrase, secretKey, salt, userId)
  const ct = await gcmEncrypt(kek, nonce, scalar, enc.encode(passphraseInfo(userId)))
  return { alg: '2skd', kdf: 'pbkdf2-sha256', i: PBKDF2_ITERATIONS, s: b64u(salt), n: b64u(nonce), ct: b64u(ct) }
}

export async function openUserKey2skd(seal: PassphraseSeal, userId: string, passphrase: string,
                                      secretKey: Uint8Array): Promise<Bytes> {
  if (seal.alg !== '2skd' || seal.i !== PBKDF2_ITERATIONS) throw new Error('unsupported seal')
  const kek = await twoSecretKek(passphrase, secretKey, unb64u(seal.s), userId)
  return gcmDecrypt(kek, unb64u(seal.n), unb64u(seal.ct), enc.encode(passphraseInfo(userId)))
}

/** 16 bytes → "CV1-" + 26 Crockford chars in groups of 4. */
export function secretKeyCode(sk: Uint8Array): string {
  if (sk.length !== 16) throw new Error('A Secret Key is 16 bytes')
  const v = BigInt('0x' + bytesToHex(sk)) << 2n
  let s = ''
  for (let i = 25; i >= 0; i--) s += CROCKFORD[Number((v >> BigInt(5 * i)) & 31n)]
  return 'CV1-' + s.match(/.{1,4}/g)!.join('-')
}

export function parseSecretKeyCode(code: string): Bytes {
  let s = code.trim().toUpperCase()
  s = s.replace(/^CV1[-\s]*/, '')
  s = s.replace(/[-\s]/g, '').replace(/O/g, '0').replace(/[IL]/g, '1')
  if (s.length !== 26) throw new Error('A Secret Key is "CV1-" and 26 characters')
  let v = 0n
  for (const ch of s) {
    const i = CROCKFORD.indexOf(ch)
    if (i < 0) throw new Error(`"${ch}" is not in a Secret Key`)
    v = (v << 5n) | BigInt(i)
  }
  return hexToBytes((v >> 2n).toString(16).padStart(32, '0'))
}

/** Minimum strength for a vault passphrase: long, and not one repeated character. */
export function passphraseProblem(p: string): string | null {
  const s = p.normalize('NFKC')
  if (s.length < 12) return 'Use at least 12 characters — a few unrelated words works well.'
  if (new Set(s).size < 5) return 'That passphrase repeats too few characters.'
  return null
}
