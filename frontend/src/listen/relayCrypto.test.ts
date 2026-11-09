// The phone's relay crypto against the vectors the laptop's Python is tested with
// (packages/canopy_listen/tests/relay_vectors.json): same keys, same frames, byte for byte.
import { describe, expect, it } from 'vitest'
import raw from '../../../packages/canopy_listen/tests/relay_vectors.json?raw'
import {
  AUDIO,
  BadFrame,
  CONTROL,
  Opener,
  Sealer,
  b64u,
  controlMessage,
  importPublic,
  newKeyPair,
  pairKeys,
  publicB64,
  sessionKeysLaptop,
  sessionKeysPhone,
  unb64u,
} from './relayCrypto'

interface Jwkish {
  d: string
  x?: string
  y?: string
  pub: string
}
interface FrameVector {
  kind: number
  body_b64u?: string
  json?: Record<string, unknown>
  frame_b64u: string
}
const V = JSON.parse(raw) as {
  session_id: string
  laptop: Jwkish
  phone_static: Jwkish
  phone_eph: Jwkish
  up: string
  down: string
  up_frames: FrameVector[]
  down_frames: FrameVector[]
  pairing_secret: string
  pair_up: string
  pair_down: string
  pair_frames: FrameVector[]
}

/** A vector's fixed private key, as WebCrypto takes it (x, y from the public point when absent). */
function privateKey(k: Jwkish): Promise<CryptoKey> {
  const pub = unb64u(k.pub)
  const jwk: JsonWebKey = {
    kty: 'EC',
    crv: 'P-256',
    d: k.d,
    x: k.x ?? b64u(pub.slice(1, 33)),
    y: k.y ?? b64u(pub.slice(33)),
  }
  return crypto.subtle.importKey('jwk', jwk, { name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits'])
}

async function sealAll(sealer: Sealer, frames: FrameVector[]): Promise<string[]> {
  return Promise.all(
    frames.map((f) => (f.json ? sealer.control(f.json) : sealer.seal(f.kind, unb64u(f.body_b64u!)))),
  ).then((out) => out.map(b64u))
}

describe('relayCrypto against relay_vectors.json', () => {
  it('derives the session keys the laptop derives', async () => {
    const phone = await sessionKeysPhone(
      await privateKey(V.phone_eph),
      await privateKey(V.phone_static),
      V.laptop.pub,
      V.session_id,
    )
    expect(b64u(phone.up)).toBe(V.up)
    expect(b64u(phone.down)).toBe(V.down)
    const laptop = await sessionKeysLaptop(await privateKey(V.laptop), V.phone_eph.pub, V.phone_static.pub, V.session_id)
    expect(b64u(laptop.up)).toBe(V.up)
    expect(b64u(laptop.down)).toBe(V.down)
  })

  it('derives the pairing keys', async () => {
    const keys = await pairKeys(unb64u(V.pairing_secret), V.session_id)
    expect(b64u(keys.up)).toBe(V.pair_up)
    expect(b64u(keys.down)).toBe(V.pair_down)
    await expect(pairKeys(new Uint8Array(31) as never, V.session_id)).rejects.toThrow()
  })

  it('seals exactly the pinned frames, each direction counting from 0', async () => {
    expect(await sealAll(new Sealer(unb64u(V.up), V.session_id), V.up_frames)).toEqual(V.up_frames.map((f) => f.frame_b64u))
    expect(await sealAll(new Sealer(unb64u(V.down), V.session_id), V.down_frames)).toEqual(
      V.down_frames.map((f) => f.frame_b64u),
    )
    expect(await sealAll(new Sealer(unb64u(V.pair_up), V.session_id), V.pair_frames)).toEqual(
      V.pair_frames.map((f) => f.frame_b64u),
    )
  })

  it('opens the pinned frames', async () => {
    const up = new Opener(unb64u(V.up), V.session_id)
    const audio = await up.open(unb64u(V.up_frames[0].frame_b64u))
    expect(audio.kind).toBe(AUDIO)
    expect(b64u(audio.body)).toBe(V.up_frames[0].body_b64u)
    const end = await up.open(unb64u(V.up_frames[1].frame_b64u))
    expect(end.kind).toBe(CONTROL)
    expect(controlMessage(end)).toEqual({ type: 'end' })
    const down = new Opener(unb64u(V.down), V.session_id)
    expect(controlMessage(await down.open(unb64u(V.down_frames[0].frame_b64u))))
      .toEqual({ type: 'ready', name: "Jonathan's MacBook" })
  })

  it('refuses a replayed or reordered frame', async () => {
    const up = new Opener(unb64u(V.up), V.session_id)
    const [first, second] = V.up_frames.map((f) => unb64u(f.frame_b64u))
    await up.open(second)
    await expect(up.open(first)).rejects.toEqual(new BadFrame('replay'))
    await expect(up.open(second)).rejects.toEqual(new BadFrame('replay'))
  })

  it('refuses a tampered frame, another session, the wrong key, and a malformed one', async () => {
    const frame = unb64u(V.up_frames[0].frame_b64u)
    const flipped = frame.slice()
    flipped[frame.length - 1] ^= 1
    await expect(new Opener(unb64u(V.up), V.session_id).open(flipped)).rejects.toEqual(new BadFrame('forged'))
    await expect(new Opener(unb64u(V.up), 'another-session').open(frame)).rejects.toEqual(new BadFrame('forged'))
    await expect(new Opener(unb64u(V.down), V.session_id).open(frame)).rejects.toEqual(new BadFrame('forged'))
    await expect(new Opener(unb64u(V.up), V.session_id).open(frame.slice(0, 20))).rejects.toEqual(new BadFrame('shape'))
    const wrongVersion = frame.slice()
    wrongVersion[0] = 2
    await expect(new Opener(unb64u(V.up), V.session_id).open(wrongVersion)).rejects.toEqual(new BadFrame('shape'))
    // A forged frame does not move the counter: the real one still opens.
    const opener = new Opener(unb64u(V.up), V.session_id)
    await expect(opener.open(flipped)).rejects.toBeInstanceOf(BadFrame)
    await expect(opener.open(frame)).resolves.toMatchObject({ kind: AUDIO })
  })
})

describe('keys made on the phone', () => {
  it('keeps the private half in the browser and round-trips a session end to end', async () => {
    const staticKey = await newKeyPair()
    const eph = await newKeyPair()
    expect(staticKey.privateKey.extractable).toBe(false)
    await expect(crypto.subtle.exportKey('jwk', staticKey.privateKey)).rejects.toThrow()
    const pub = await publicB64(staticKey.publicKey)
    expect(unb64u(pub)).toHaveLength(65)
    await importPublic(pub)

    const laptop = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits'])
    const laptopPub = await publicB64(laptop.publicKey)
    const sid = '6f1c2a8e-0000-4000-8000-0000000000aa'
    const phoneSide = await sessionKeysPhone(eph.privateKey, staticKey.privateKey, laptopPub, sid)
    const laptopSide = await sessionKeysLaptop(laptop.privateKey, await publicB64(eph.publicKey), pub, sid)
    expect(b64u(phoneSide.up)).toBe(b64u(laptopSide.up))
    const sealer = new Sealer(phoneSide.up, sid)
    const opener = new Opener(laptopSide.up, sid)
    // Sealed concurrently, still opened in order: counters come out rising.
    const frames = await Promise.all([1, 2, 3].map((n) => sealer.control({ type: 'rename', speaker: n, name: `S${n}` })))
    for (const [i, f] of frames.entries()) {
      expect(controlMessage(await opener.open(f))).toEqual({ type: 'rename', speaker: i + 1, name: `S${i + 1}` })
    }
  })
})
