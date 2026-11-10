// Pairing and a meeting, end to end against a fake laptop that holds the real keys:
// the page's state machine, with only REST, the socket and the microphone faked.
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/problem'
import type { ListenHello } from '@/api/listen'
import { Meeting, pairByCode, pairPhone, type ListenDeps } from './controller'
import type { PairInvite } from './pairLink'
import {
  AUDIO,
  b64u,
  controlMessage,
  handshakeFrame,
  newNonce,
  pairCode,
  pairCommit,
  readHandshake,
  newKeyPair,
  Opener,
  pairKeys,
  publicB64,
  Sealer,
  sessionKeysLaptop,
  unb64u,
} from './relayCrypto'
import { memoryStore, settle, socketFactory } from './testing'

const SID = '6f1c2a8e-0000-4000-8000-0000000000bb'

function deps(overrides: Partial<ListenDeps> = {}) {
  const sockets = socketFactory()
  const hellos: ListenHello[] = []
  const ended: string[] = []
  const mic: { chunk: (pcm: Uint8Array) => void; stopped: boolean } = { chunk: () => undefined, stopped: false }
  const d: ListenDeps = {
    openSession: vi.fn(async (_runner, _kind, hello) => {
      hellos.push(hello)
      return SID
    }),
    endSession: vi.fn(async (id: string) => {
      ended.push(id)
    }),
    listRunners: vi.fn(async () => []),
    store: memoryStore(),
    socket: sockets.make,
    wsUrl: (path) => `wss://canopy.test/${path}`,
    label: 'iPhone',
    startMic: async (onChunk) => {
      mic.chunk = onChunk
      return { stop: () => void (mic.stopped = true) }
    },
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
    ...overrides,
  }
  return { d, sockets, hellos, ended, mic }
}

describe('pairPhone', () => {
  it('sends its static key under the pairing keys once the laptop is there, and stores the laptop', async () => {
    const { d, sockets, hellos, ended } = deps()
    const laptop = await newKeyPair()
    const invite: PairInvite = {
      runnerId: 'runner-1', laptopPub: await publicB64(laptop.publicKey), secret: new Uint8Array(32).fill(5) as never,
      laptopName: 'MBP', expiresAt: Date.now() / 1000 + 600,
    }
    const steps: string[] = []
    const done = pairPhone(invite, d, (s) => steps.push(s))
    await vi.waitFor(() => expect(sockets.sockets).toHaveLength(1))
    const ws = sockets.sockets[0]
    expect(ws.url).toBe(`wss://canopy.test/ws/listen/${SID}/?role=phone`)
    expect(hellos[0].phone_key).toBeNull()
    expect(unb64u(hellos[0].phone_eph)).toHaveLength(65)
    ws.open()
    ws.text({ type: 'peer', role: 'laptop', connected: false })
    await settle()
    expect(ws.sent).toHaveLength(0) // nobody to hear it yet
    ws.text({ type: 'peer', role: 'laptop', connected: true })
    await vi.waitFor(() => expect(ws.sent).toHaveLength(1))

    const keys = await pairKeys(invite.secret, SID)
    const pair = controlMessage(await new Opener(keys.up, SID).open(ws.sent[0]))!
    const phoneKey = await publicB64((await d.store.phoneKey()).publicKey)
    expect(pair).toEqual({ type: 'pair', phone_key: phoneKey, label: 'iPhone' })
    ws.binary(await new Sealer(keys.down, SID).control({ type: 'paired', name: "Jonathan's MacBook" }))
    const pairing = await done
    expect(pairing).toEqual({ runner_id: 'runner-1', laptop_pub: invite.laptopPub, laptop_name: "Jonathan's MacBook" })
    expect(await d.store.pairing()).toEqual(pairing)
    expect(steps).toEqual(['connecting', 'waiting', 'done'])
    expect(ended).toEqual([SID])
  })

  it('says why when the laptop refuses, or the code expired, or the laptop is offline', async () => {
    const laptop = await newKeyPair()
    const base: PairInvite = {
      runnerId: 'r', laptopPub: await publicB64(laptop.publicKey), secret: new Uint8Array(32) as never,
      laptopName: 'MBP', expiresAt: Date.now() / 1000 + 600,
    }
    const refused = deps()
    const p = pairPhone(base, refused.d, () => undefined)
    await vi.waitFor(() => expect(refused.sockets.sockets).toHaveLength(1))
    refused.sockets.sockets[0].open()
    const keys = await pairKeys(base.secret, SID)
    refused.sockets.sockets[0].binary(await new Sealer(keys.down, SID).control({ type: 'refused', reason: 'code already used' }))
    await expect(p).rejects.toThrow('MBP refused this phone: code already used.')
    expect(await refused.d.store.pairing()).toBeNull()

    await expect(pairPhone({ ...base, expiresAt: 1 }, deps().d, () => undefined)).rejects.toThrow(/expired/)

    const offline = deps({ openSession: async () => Promise.reject(new ApiError(409, 'That computer is offline')) })
    await expect(pairPhone(base, offline.d, () => undefined)).rejects.toThrow('MBP is offline. Open it and make sure Canopy Runner is running.')
  })
})

describe('pairByCode', () => {
  /** A laptop holding a real key: answers the commitment, checks it, and seals 'paired'. */
  async function laptopAnswers(allow: boolean | 'busy') {
    const { d, sockets, hellos, ended } = deps()
    const laptop = await newKeyPair()
    const laptopPub = await publicB64(laptop.publicKey)
    const steps: Array<[string, string | undefined]> = []
    const done = pairByCode({ id: 'runner-1', name: 'MBP' }, d, (s, code) => steps.push([s, code]))
    await vi.waitFor(() => expect(sockets.sockets).toHaveLength(1))
    const ws = sockets.sockets[0]
    ws.open()
    const hello = hellos[0]
    expect(hello.phone_key).toBeNull()
    if (allow === 'busy') {
      ws.binary(handshakeFrame({ type: 'refused', reason: 'busy' }))
      return { done, steps, ended, d }
    }
    const laptopNonce = newNonce()
    ws.binary(handshakeFrame({ type: 'hello', laptop_key: laptopPub, nonce: b64u(laptopNonce), name: 'MBP' }))
    await vi.waitFor(() => expect(ws.sent).toHaveLength(1))
    const reveal = readHandshake(ws.sent[0])!
    expect(reveal.type).toBe('reveal')
    // What the laptop checks: the key revealed is the one committed to before it saw ours.
    expect(await pairCommit(reveal.phone_key as string, unb64u(reveal.nonce as string))).toBe(hello.commit)
    const code = await pairCode(SID, reveal.phone_key as string, laptopPub, unb64u(reveal.nonce as string), laptopNonce)
    await vi.waitFor(() => expect(steps.at(-1)).toEqual(['confirm', code]))
    if (allow) {
      const keys = await sessionKeysLaptop(laptop.privateKey, hello.phone_eph, reveal.phone_key as string, SID)
      ws.binary(await new Sealer(keys.down, SID).control({ type: 'paired', name: "Jonathan's MacBook" }))
    } else {
      ws.binary(handshakeFrame({ type: 'refused', reason: 'declined' }))
    }
    return { done, steps, ended, d, laptopPub }
  }

  it('commits, reveals, shows the code the laptop shows, and keeps the laptop once it seals "paired"', async () => {
    const { done, steps, ended, d, laptopPub } = await laptopAnswers(true)
    const pairing = await done
    expect(pairing).toEqual({ runner_id: 'runner-1', laptop_pub: laptopPub, laptop_name: "Jonathan's MacBook" })
    expect(await d.store.pairing()).toEqual(pairing)
    expect(steps.map(([s]) => s)).toEqual(['connecting', 'waiting', 'confirm', 'done'])
    expect(ended).toEqual([SID])
  })

  it('says so when it is declined or another code is already showing, and keeps nothing', async () => {
    const declined = await laptopAnswers(false)
    await expect(declined.done).rejects.toThrow(/declined on MBP/)
    expect(await declined.d.store.pairing()).toBeNull()
    const busy = await laptopAnswers('busy')
    await expect(busy.done).rejects.toThrow(/already showing a pairing code/)
  })

  it('ignores a "paired" it cannot open: only the laptop it saw can say it', async () => {
    const { d, sockets } = deps()
    const done = pairByCode({ id: 'runner-1', name: 'MBP' }, d, () => undefined)
    await vi.waitFor(() => expect(sockets.sockets).toHaveLength(1))
    const ws = sockets.sockets[0]
    ws.open()
    const real = await newKeyPair()
    ws.binary(handshakeFrame({ type: 'hello', laptop_key: await publicB64(real.publicKey), nonce: b64u(newNonce()), name: 'MBP' }))
    await vi.waitFor(() => expect(ws.sent).toHaveLength(1))
    ws.binary(await new Sealer(new Uint8Array(32).fill(9) as never, SID).control({ type: 'paired', name: 'Impostor' }))
    await settle()
    expect(await d.store.pairing()).toBeNull()
    ws.drop()
    await expect(done).rejects.toThrow()
  })
})

describe('Meeting', () => {
  async function started() {
    const env = deps()
    const laptop = await newKeyPair()
    const pairing = { runner_id: 'runner-1', laptop_pub: await publicB64(laptop.publicKey), laptop_name: 'MBP' }
    const meeting = new Meeting(pairing, env.d)
    void meeting.begin()
    await vi.waitFor(() => expect(env.sockets.sockets).toHaveLength(1))
    const ws = env.sockets.sockets[0]
    ws.open()
    const hello = env.hellos[0]
    const keys = await sessionKeysLaptop(laptop.privateKey, hello.phone_eph, hello.phone_key!, SID)
    return { ...env, meeting, ws, fromPhone: new Opener(keys.up, SID), toPhone: new Sealer(keys.down, SID) }
  }

  it('records: no audio before ready, then start, audio, transcript, rename, end and saved', async () => {
    const { meeting, ws, mic, fromPhone, toPhone, ended } = await started()
    expect(meeting.view.status).toBe('waiting')
    mic.chunk(new Uint8Array([1, 2]))
    await settle()
    expect(ws.sent).toHaveLength(0)

    ws.binary(await toPhone.control({ type: 'ready', name: "Jonathan's MacBook" }))
    await vi.waitFor(() => expect(ws.sent).toHaveLength(2))
    expect(meeting.view.status).toBe('recording')
    expect(meeting.view.laptopName).toBe("Jonathan's MacBook")
    expect(controlMessage(await fromPhone.open(ws.sent[0]))).toEqual({ type: 'start', notice_accepted: true })
    const held = await fromPhone.open(ws.sent[1])
    expect([held.kind, ...held.body]).toEqual([AUDIO, 1, 2])

    ws.binary(await toPhone.control({ type: 'segment', start: 0, end: 1.5, speaker: 1, text: 'Hello there' }))
    ws.binary(await toPhone.control({ type: 'speakers', speakers: [{ number: 1, name: 'Ana' }] }))
    ws.binary(await toPhone.control({ type: 'chat', url: 'https://canopy.test/w/x/sessions/1' }))
    ws.binary(await toPhone.control({ type: 'chat', url: 'javascript:alert(1)' }))
    await settle()
    expect(meeting.view.segments).toEqual([{ start: 0, end: 1.5, speaker: 1, text: 'Hello there' }])
    expect(meeting.view.speakers).toEqual({ 1: 'Ana' })
    expect(meeting.view.chatUrl).toBeUndefined() // the unsafe link replaced the good one: never shown

    meeting.rename(1, '  Ana Silva ')
    await vi.waitFor(() => expect(ws.sent).toHaveLength(3))
    expect(controlMessage(await fromPhone.open(ws.sent[2]))).toEqual({ type: 'rename', speaker: 1, name: 'Ana Silva' })

    meeting.end()
    expect(mic.stopped).toBe(true)
    expect(meeting.view.status).toBe('ending')
    await vi.waitFor(() => expect(ws.sent).toHaveLength(4))
    expect(controlMessage(await fromPhone.open(ws.sent[3]))).toEqual({ type: 'end' })
    ws.binary(await toPhone.control({ type: 'saved', url: 'https://canopy.test/w/x/meetings/9' }))
    await vi.waitFor(() => expect(meeting.view.status).toBe('saved'))
    expect(meeting.view.savedUrl).toBe('https://canopy.test/w/x/meetings/9')
    expect(ended).toEqual([SID])
    expect(ws.closedWith).toBe(1000)
  })

  it('a laptop that fails the recording, or a dropped connection, says what happened', async () => {
    const one = await started()
    one.ws.binary(await one.toPhone.control({ type: 'ready', name: 'MBP' }))
    one.ws.binary(await one.toPhone.control({ type: 'state', state: 'failed', detail: 'No models installed.' }))
    await vi.waitFor(() => expect(one.meeting.view.status).toBe('failed'))
    expect(one.meeting.view.error).toBe('No models installed.')
    expect(one.mic.stopped).toBe(true)

    const two = await started()
    two.ws.drop(4009)
    await vi.waitFor(() => expect(two.meeting.view.status).toBe('failed'))
    expect(two.meeting.view.error).toMatch(/another phone or tab/)
  })

  it('a busy laptop is refused before anything is sent', async () => {
    const { d } = deps({ openSession: async () => Promise.reject(new ApiError(409, 'A meeting is already running on that computer')) })
    const laptop = await newKeyPair()
    const meeting = new Meeting({ runner_id: 'r', laptop_pub: await publicB64(laptop.publicKey), laptop_name: 'MBP' }, d)
    await meeting.begin()
    expect(meeting.view.status).toBe('failed')
    expect(meeting.view.error).toBe('MBP is already recording a meeting. End it there first.')
  })

  it('a microphone that is refused stops it before any session opens', async () => {
    const { d } = deps({ startMic: async () => Promise.reject(new Error('Microphone access is off for this site.')) })
    const laptop = await newKeyPair()
    const meeting = new Meeting({ runner_id: 'r', laptop_pub: await publicB64(laptop.publicKey), laptop_name: 'MBP' }, d)
    await meeting.begin()
    expect(meeting.view).toMatchObject({ status: 'failed', error: 'Microphone access is off for this site.' })
    expect(d.openSession).not.toHaveBeenCalled()
  })

  it('discarding tells the laptop, then closes the session', async () => {
    const { meeting, ws, fromPhone, toPhone, ended } = await started()
    ws.binary(await toPhone.control({ type: 'ready', name: 'MBP' }))
    await vi.waitFor(() => expect(meeting.view.status).toBe('recording'))
    vi.useFakeTimers({ toFake: ['setTimeout'] })
    try {
      meeting.discard()
      await vi.waitFor(() => expect(ws.sent).toHaveLength(2))
      expect(controlMessage(await fromPhone.open(ws.sent[1]))).toEqual({ type: 'discard' })
      await vi.advanceTimersByTimeAsync(1600)
      expect(meeting.view.status).toBe('discarded')
      expect(ended).toEqual([SID])
    } finally {
      vi.useRealTimers()
    }
  })
})
