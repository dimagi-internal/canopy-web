/**
 * End-to-end encryption between this phone and its paired laptop, through Canopy's relay.
 *
 * The phone's side of the spec in `packages/canopy_listen/canopy_listen/relaycrypto.py`
 * (its docstring is the spec); `relay_vectors.json` beside it pins both sides to the same
 * bytes (relayCrypto.test.ts). WebCrypto only: ECDH P-256, HKDF-SHA256, AES-256-GCM.
 *
 * Session keys: ikm = ECDH(phone_eph, laptop) || ECDH(phone_static, laptop);
 * HKDF(ikm, salt = session id, info = "canopy-listen relay v1", 64) → up | down.
 * Pair keys: HKDF(pairing secret, salt = session id, info = "canopy-listen pair v1", 64).
 * Frame: 0x01 | counter (8, BE) | AES-GCM(key, nonce = 0000 | counter, kind | body, aad = session id).
 */

export type Bytes = Uint8Array<ArrayBuffer>

export const VERSION = 0x01
export const AUDIO = 0x01
export const CONTROL = 0x02
export const MAX_FRAME = 64 * 1024
const SESSION_INFO = 'canopy-listen relay v1'
const PAIR_INFO = 'canopy-listen pair v1'
const ECDH_P256 = { name: 'ECDH', namedCurve: 'P-256' } as const

const subtle = () => globalThis.crypto.subtle
const utf8 = (text: string): Bytes => new TextEncoder().encode(text) as Bytes

/** A frame that doesn't open: forged, malformed or replayed. Its message is a reason code, never content. */
export class BadFrame extends Error {
  readonly reason: 'shape' | 'replay' | 'forged'

  constructor(reason: 'shape' | 'replay' | 'forged') {
    super(reason)
    this.name = 'BadFrame'
    this.reason = reason
  }
}

export function b64u(bytes: Uint8Array): string {
  let bin = ''
  for (const b of bytes) bin += String.fromCharCode(b)
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

export function unb64u(text: string): Bytes {
  const pad = text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (text.length % 4)) % 4)
  const bin = atob(pad)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out as Bytes
}

function concat(...parts: Uint8Array[]): Bytes {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0))
  let at = 0
  for (const p of parts) {
    out.set(p, at)
    at += p.length
  }
  return out as Bytes
}

// -- keys -----------------------------------------------------------------------------

/** A new ECDH P-256 key whose private half can never leave this browser. */
export function newKeyPair(): Promise<CryptoKeyPair> {
  return subtle().generateKey(ECDH_P256, false, ['deriveBits']) as Promise<CryptoKeyPair>
}

/** The 65-byte uncompressed public point, base64url without padding. */
export async function publicB64(key: CryptoKey): Promise<string> {
  return b64u(new Uint8Array(await subtle().exportKey('raw', key)))
}

export async function importPublic(text: string): Promise<CryptoKey> {
  const raw = unb64u(text)
  if (raw.length !== 65 || raw[0] !== 4) throw new Error('not an uncompressed P-256 point')
  return subtle().importKey('raw', raw, ECDH_P256, true, [])
}

async function ecdh(privateKey: CryptoKey, publicKey: CryptoKey): Promise<Bytes> {
  return new Uint8Array(await subtle().deriveBits({ name: 'ECDH', public: publicKey }, privateKey, 256)) as Bytes
}

export interface DirectionKeys {
  /** phone → laptop */
  up: Bytes
  /** laptop → phone */
  down: Bytes
}

async function hkdf(ikm: Bytes, sessionId: string, info: string): Promise<DirectionKeys> {
  const base = await subtle().importKey('raw', ikm, 'HKDF', false, ['deriveBits'])
  const okm = new Uint8Array(
    await subtle().deriveBits({ name: 'HKDF', hash: 'SHA-256', salt: utf8(sessionId), info: utf8(info) }, base, 512),
  )
  return { up: okm.slice(0, 32) as Bytes, down: okm.slice(32) as Bytes }
}

/** (up, down) from the two ECDH secrets, in the spec's order: ephemeral term first. */
export function sessionKeysFromSecrets(ephSecret: Bytes, staticSecret: Bytes, sessionId: string) {
  return hkdf(concat(ephSecret, staticSecret), sessionId, SESSION_INFO)
}

/** A session's keys, as the phone computes them. */
export async function sessionKeysPhone(
  phoneEph: CryptoKey,
  phoneStatic: CryptoKey,
  laptopPub: string,
  sessionId: string,
): Promise<DirectionKeys> {
  const laptop = await importPublic(laptopPub)
  return sessionKeysFromSecrets(await ecdh(phoneEph, laptop), await ecdh(phoneStatic, laptop), sessionId)
}

/** The same keys from the laptop's side (tests; the laptop's own code is Python). */
export async function sessionKeysLaptop(
  laptopStatic: CryptoKey,
  phoneEphPub: string,
  phoneStaticPub: string,
  sessionId: string,
): Promise<DirectionKeys> {
  const eph = await ecdh(laptopStatic, await importPublic(phoneEphPub))
  const stat = await ecdh(laptopStatic, await importPublic(phoneStaticPub))
  return sessionKeysFromSecrets(eph, stat, sessionId)
}

/** Keys for the one pairing frame each way, from the QR code's 32-byte secret. */
export async function pairKeys(pairingSecret: Bytes, sessionId: string): Promise<DirectionKeys> {
  if (pairingSecret.length !== 32) throw new Error('a pairing secret is 32 bytes')
  return hkdf(pairingSecret, sessionId, PAIR_INFO)
}

// -- frames ---------------------------------------------------------------------------

function nonceFor(counterBytes: Uint8Array): Bytes {
  return concat(new Uint8Array(4), counterBytes)
}

function aesKey(raw: Bytes, usage: KeyUsage): Promise<CryptoKey> {
  return subtle().importKey('raw', raw, 'AES-GCM', false, [usage])
}

/**
 * One direction's sender. Frames come out in the order `seal` was called (each waits for
 * the one before it), so a caller that sends each as it resolves keeps counters rising.
 */
export class Sealer {
  private counter = 0n
  private queue: Promise<unknown> = Promise.resolve()
  private readonly key: Promise<CryptoKey>
  private readonly aad: Bytes

  constructor(rawKey: Bytes, sessionId: string) {
    this.key = aesKey(rawKey, 'encrypt')
    this.aad = utf8(sessionId)
  }

  seal(kind: number, body: Uint8Array): Promise<Bytes> {
    const counter = this.counter
    this.counter += 1n
    const next = this.queue.then(async () => {
      const head = new Uint8Array(8)
      new DataView(head.buffer).setBigUint64(0, counter, false)
      const sealed = await subtle().encrypt(
        { name: 'AES-GCM', iv: nonceFor(head), additionalData: this.aad },
        await this.key,
        concat(new Uint8Array([kind]), body),
      )
      return concat(new Uint8Array([VERSION]), head, new Uint8Array(sealed))
    })
    this.queue = next.catch(() => undefined)
    return next
  }

  /** A control message: compact JSON, as the laptop writes it. */
  control(message: Record<string, unknown>): Promise<Bytes> {
    return this.seal(CONTROL, utf8(JSON.stringify(message)))
  }
}

export interface Opened {
  kind: number
  body: Bytes
}

/** One direction's receiver: refuses anything forged, malformed, reordered or replayed. */
export class Opener {
  private last = -1n
  private queue: Promise<unknown> = Promise.resolve()
  private readonly key: Promise<CryptoKey>
  private readonly aad: Bytes

  constructor(rawKey: Bytes, sessionId: string) {
    this.key = aesKey(rawKey, 'decrypt')
    this.aad = utf8(sessionId)
  }

  open(frame: Uint8Array): Promise<Opened> {
    const next = this.queue.then(() => this.openNow(frame))
    this.queue = next.catch(() => undefined)
    return next
  }

  private async openNow(frame: Uint8Array): Promise<Opened> {
    if (frame.length < 1 + 8 + 16 + 1 || frame.length > MAX_FRAME || frame[0] !== VERSION) {
      throw new BadFrame('shape')
    }
    const head = frame.slice(1, 9)
    const counter = new DataView(head.buffer).getBigUint64(0, false)
    if (counter <= this.last) throw new BadFrame('replay')
    let plain: Uint8Array
    try {
      plain = new Uint8Array(
        await subtle().decrypt(
          { name: 'AES-GCM', iv: nonceFor(head), additionalData: this.aad },
          await this.key,
          frame.slice(9),
        ),
      )
    } catch {
      throw new BadFrame('forged')
    }
    this.last = counter
    return { kind: plain[0], body: plain.slice(1) as Bytes }
  }
}

/** A control message's JSON, or null for audio or anything that isn't an object. */
export function controlMessage(opened: Opened): Record<string, unknown> | null {
  if (opened.kind !== CONTROL) return null
  try {
    const value: unknown = JSON.parse(new TextDecoder().decode(opened.body))
    return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null
  } catch {
    return null
  }
}
