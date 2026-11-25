/**
 * The two things the phone does over the relay — pair once, then record meetings — as
 * plain classes the page drives. Everything outside (REST, the socket, storage, the
 * microphone) comes in through `ListenDeps`, so the flows test without a browser.
 */
import { ApiError } from '@/api/problem'
import type { ListenHello, ListenKind, ListenRunner } from '@/api/listen'
import type { MicCapture, OnPause } from './audio'
import type { PairInvite } from './pairLink'
import { isExpired } from './pairLink'
import { PhoneRelay, type Control, type LostReason, type SocketFactory } from './relay'
import {
  b64u,
  BadFrame,
  controlMessage,
  HANDSHAKE,
  handshakeFrame,
  importPublic,
  newKeyPair,
  newNonce,
  NONCE_BYTES,
  Opener,
  pairCode,
  pairCommit,
  pairKeys,
  publicB64,
  readHandshake,
  Sealer,
  sessionKeysPhone,
  unb64u,
} from './relayCrypto'
import type { ListenStore, Pairing } from './store'

export interface ListenDeps {
  openSession(runnerId: string, kind: ListenKind, hello: ListenHello): Promise<string>
  endSession(id: string): Promise<unknown>
  listRunners(): Promise<ListenRunner[]>
  store: ListenStore
  socket: SocketFactory
  wsUrl(path: string): string
  label: string
  startMic(onChunk: (pcm: Uint8Array) => void, onLevel: (level: number) => void, onPause?: OnPause): Promise<MicCapture>
  /** A call in another tab plus the microphone; absent where the browser can't share a tab. */
  startTab?: (onChunk: (pcm: Uint8Array) => void, onLevel: (level: number) => void, onPause?: OnPause) => Promise<MicCapture>
  /** Timers, injectable for tests. */
  setTimeout(fn: () => void, ms: number): unknown
  clearTimeout(handle: unknown): void
}

/**
 * How long the laptop has to answer. A laptop that is awake and online connects within a
 * few seconds, so after SLOW_LAPTOP_MS the page says it hasn't yet, and keeps waiting.
 * It gives up at LAPTOP_TIMEOUT_MS: the runner reopens a control socket that went quiet
 * within about 75 s and then polls for waiting sessions, so a laptop that just woke up can
 * still make it. Once connected, READY_TIMEOUT_MS covers Canopy Listen starting up.
 */
export const SLOW_LAPTOP_MS = 15_000
export const LAPTOP_TIMEOUT_MS = 100_000
export const READY_TIMEOUT_MS = 45_000
export const PAIR_TIMEOUT_MS = 60_000
export const SAVE_TIMEOUT_MS = 90_000

const LAPTOP_FAILURES: Record<string, (name: string) => string> = {
  recorder_stopped: (n) => `The recorder on ${n} stopped unexpectedly. What it heard so far is saved there.`,
  busy: (n) => `${n} is already recording a meeting. End it there first.`,
  notice_not_accepted: () => 'The recording notice has to be acknowledged first.',
  app_did_not_start: (n) => `Voice assistant didn't start on ${n}. Check it's installed there.`,
  did_not_start: (n) => `${n} started Voice assistant, but it never began recording. Try again.`,
  update_page: (n) => `Reload this page: Canopy Listen on ${n} needs its newest version.`,
}

/** A laptop's `failed` detail in words: its codes, or its own sentence. */
export function laptopFailure(detail: string, name: string): string {
  if (!detail) return `${name} stopped the recording.`
  const known = LAPTOP_FAILURES[detail]
  return known ? known(name) : detail
}

/** A REST refusal in words for this page. */
export function openRefusal(e: unknown, laptopName: string): string {
  if (e instanceof ApiError) {
    if (e.status === 404) return `${laptopName} isn't one of your computers on this account. Check you're signed in as yourself.`
    if (e.status === 409) {
      return /already/i.test(e.message)
        ? `${laptopName} is already recording a meeting. End it there first.`
        : `${laptopName} is offline. Open it and make sure Canopy Runner is running.`
    }
    if (e.status === 401) return 'You were signed out. Reload to sign in again.'
    return e.message || 'Canopy refused the request.'
  }
  return 'Could not reach Canopy. Check your connection.'
}

export function lostInWords(why: LostReason): string {
  switch (why) {
    case 'taken':
      return 'This meeting is open on another phone or tab.'
    case 'too_much':
      return 'The connection was closed for sending too much at once.'
    case 'signed_out':
      return 'You were signed out of Canopy.'
    case 'refused':
      return "Canopy wouldn't connect this phone to that session."
    default:
      return 'The connection to Canopy dropped and could not be restored.'
  }
}

function safeUrl(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.toString() : undefined
  } catch {
    return undefined
  }
}

/** What to say when a laptop never connected to a session at all. */
export function notAnswering(name: string): string {
  return `${name} isn't answering. Make sure it's awake, online and running Canopy, then try again.`
}

// -- pairing -------------------------------------------------------------------------

export type PairStep = 'connecting' | 'waiting' | 'confirm' | 'done'

/** Pair this phone with the laptop that showed `invite`. Resolves with what was stored. */
export async function pairPhone(invite: PairInvite, deps: ListenDeps, onStep: (step: PairStep) => void): Promise<Pairing> {
  if (isExpired(invite)) throw new Error('This pairing code has expired. Make a new one on your laptop.')
  onStep('connecting')
  const phone = await deps.store.phoneKey()
  const eph = await newKeyPair()
  const hello: ListenHello = { phone_eph: await publicB64(eph.publicKey), phone_key: null, label: deps.label }
  let sid: string
  try {
    sid = await deps.openSession(invite.runnerId, 'pair', hello)
  } catch (e) {
    throw new Error(openRefusal(e, invite.laptopName))
  }
  const keys = await pairKeys(invite.secret, sid)
  const phoneKey = await publicB64(phone.publicKey)
  try {
    const answer = await new Promise<Control>((resolve, reject) => {
      let sent = false
      const timer = deps.setTimeout(() => reject(new Error(`${invite.laptopName} didn't answer. Keep Canopy Listen open there and try again.`)), PAIR_TIMEOUT_MS)
      const finish = (fn: () => void) => {
        deps.clearTimeout(timer)
        relay.close()
        fn()
      }
      const relay = new PhoneRelay(deps.wsUrl(`ws/listen/${sid}/?role=phone`), new Sealer(keys.up, sid), new Opener(keys.down, sid), {
        onPeer(connected) {
          if (!connected || sent) return
          sent = true
          onStep('waiting')
          void relay.sendControl({ type: 'pair', phone_key: phoneKey, label: deps.label })
        },
        onControl(message) {
          if (message.type === 'paired') finish(() => resolve(message))
          else if (message.type === 'refused') {
            const reason = typeof message.reason === 'string' && message.reason ? `: ${message.reason}` : ''
            finish(() => reject(new Error(`${invite.laptopName} refused this phone${reason}.`)))
          }
        },
        onEnded() {
          finish(() => reject(new Error('The pairing session ended before the laptop answered.')))
        },
        onLost(why) {
          finish(() => reject(new Error(lostInWords(why))))
        },
      }, deps.socket)
      relay.connect().catch(() => finish(() => reject(new Error('Could not connect to Canopy.'))))
    })
    const pairing: Pairing = {
      runner_id: invite.runnerId,
      laptop_pub: invite.laptopPub,
      laptop_name: typeof answer.name === 'string' && answer.name ? answer.name : invite.laptopName,
    }
    await deps.store.setPairing(pairing)
    onStep('done')
    return pairing
  } finally {
    void deps.endSession(sid).catch(() => undefined)
  }
}

/** The laptop's dialog waits two minutes; give it a little longer than that. */
export const PAIR_CODE_TIMEOUT_MS = 150_000

const CODE_REFUSALS: Record<string, (name: string) => string> = {
  busy: (name) => `${name} is already showing a pairing code. Answer that one first, then try again.`,
  declined: (name) => `Pairing was declined on ${name}, or nobody answered there in time.`,
  wait: (name) => `A pairing was just declined on ${name}. Wait half a minute, then try again.`,
}

/**
 * Pair from canopy-web itself, no QR: the laptop shows a dialog with six digits, and the
 * person allows it if this page shows the same ones. `onStep('confirm', code)` is when to
 * show them. The relay sees the handshake but can't get in the middle without the codes
 * differing (relaycrypto.py, "PAIRING BY CODE").
 */
export async function pairByCode(
  runner: { id: string; name: string },
  deps: ListenDeps,
  onStep: (step: PairStep, code?: string) => void,
  onSlow?: () => void,
): Promise<Pairing> {
  onStep('connecting')
  const phone = await deps.store.phoneKey()
  const eph = await newKeyPair()
  const nonce = newNonce()
  const phoneKey = await publicB64(phone.publicKey)
  const hello: ListenHello = {
    phone_eph: await publicB64(eph.publicKey), phone_key: null, label: deps.label,
    commit: await pairCommit(phoneKey, nonce),
  }
  let sid: string
  try {
    sid = await deps.openSession(runner.id, 'pair', hello)
  } catch (e) {
    throw new Error(openRefusal(e, runner.name))
  }
  try {
    const { laptopKey, name } = await new Promise<{ laptopKey: string; name: string }>((resolve, reject) => {
      let finished = false
      let laptopKey = ''
      let opener: Opener | null = null
      let queue = Promise.resolve()
      const ws = deps.socket(deps.wsUrl(`ws/listen/${sid}/?role=phone`))
      let laptopHere = false
      const finish = (fn: () => void) => {
        if (finished) return
        finished = true
        deps.clearTimeout(timer)
        deps.clearTimeout(slow)
        deps.clearTimeout(absent)
        ws.onclose = null
        ws.close(1000)
        fn()
      }
      const fail = (message: string) => finish(() => reject(new Error(message)))
      const timer = deps.setTimeout(() => fail(`${runner.name} didn't answer in time. Try again.`), PAIR_CODE_TIMEOUT_MS)
      const slow = deps.setTimeout(() => !laptopHere && !laptopKey && onSlow?.(), SLOW_LAPTOP_MS)
      const absent = deps.setTimeout(() => !laptopHere && !laptopKey && fail(notAnswering(runner.name)), LAPTOP_TIMEOUT_MS)

      const onFrame = async (bytes: Uint8Array) => {
        if (bytes[0] === HANDSHAKE) {
          const m = readHandshake(bytes)
          if (m?.type === 'refused') {
            const why = typeof m.reason === 'string' ? m.reason : ''
            fail((CODE_REFUSALS[why] ?? ((n: string) => `${n} refused this pairing.`))(runner.name))
            return
          }
          laptopHere = true
          if (m?.type !== 'hello' || laptopKey) return
          const laptopNonce = typeof m.nonce === 'string' ? unb64u(m.nonce) : new Uint8Array()
          if (typeof m.laptop_key !== 'string' || laptopNonce.length !== NONCE_BYTES) {
            fail(`${runner.name} answered with something this page doesn't understand. Update Canopy Listen there.`)
            return
          }
          await importPublic(m.laptop_key) // throws on anything that isn't a P-256 point
          laptopKey = m.laptop_key
          const keys = await sessionKeysPhone(eph.privateKey, phone.privateKey, laptopKey, sid)
          opener = new Opener(keys.down, sid)
          ws.send(handshakeFrame({ type: 'reveal', phone_key: phoneKey, nonce: b64u(nonce), label: deps.label }))
          onStep('confirm', await pairCode(sid, phoneKey, laptopKey, nonce, laptopNonce))
          return
        }
        if (!opener) return
        let message: Control | null = null
        try {
          message = controlMessage(await opener.open(bytes))
        } catch (e) {
          if (e instanceof BadFrame) return // forged: ignored, the codes will say so
          throw e
        }
        if (message?.type === 'paired') {
          const said = typeof message.name === 'string' && message.name ? message.name : runner.name
          finish(() => resolve({ laptopKey, name: said }))
        }
      }

      ws.binaryType = 'arraybuffer'
      ws.onopen = () => onStep('waiting')
      ws.onerror = () => undefined
      ws.onmessage = (ev) => {
        const data = ev.data
        if (typeof data === 'string') {
          try {
            const note = JSON.parse(data) as Control
            if (note?.type === 'peer' && note.role === 'laptop' && note.connected === true) laptopHere = true
            if (note?.type === 'ended') fail('The pairing session ended before it finished.')
          } catch {
            // not a notice
          }
          return
        }
        const bytes = data instanceof ArrayBuffer ? new Uint8Array(data) : data instanceof Uint8Array ? data : null
        if (bytes?.length) {
          queue = queue.then(() => onFrame(bytes)).catch(() => fail(`${runner.name} sent a key this page can't use.`))
        }
      }
      ws.onclose = (ev) => {
        const fatal: Record<number, LostReason> = { 4001: 'signed_out', 4003: 'refused', 4009: 'taken' }
        fail(fatal[ev.code] ? lostInWords(fatal[ev.code]) : 'The connection closed before pairing finished. Try again.')
      }
    })
    const pairing: Pairing = { runner_id: runner.id, laptop_pub: laptopKey, laptop_name: name }
    await deps.store.setPairing(pairing)
    onStep('done')
    return pairing
  } finally {
    void deps.endSession(sid).catch(() => undefined)
  }
}

// -- a meeting -----------------------------------------------------------------------

export interface Segment {
  start: number
  end: number
  speaker: number
  text: string
}

/** Where a meeting's sound comes from: this device's microphone, or a call in another tab. */
export type AudioSource = 'mic' | 'tab'

/**
 * `saved`: the laptop said Voice assistant saved it. `unsaved`: it ended, but no save was
 * confirmed (`note` says why); the recording is still on the laptop.
 */
export type MeetingStatus = 'connecting' | 'waiting' | 'recording' | 'ending' | 'saved' | 'unsaved' | 'discarded' | 'failed'

export interface MeetingView {
  status: MeetingStatus
  laptopName: string
  /** The laptop's own word for where it is (starting, recording, ending…). */
  laptopState: string
  segments: Segment[]
  /** Words heard but not final yet: shown as they are said, and may still change. */
  partial: string
  speakers: Record<number, string>
  chatUrl?: string
  savedUrl?: string
  error?: string
  reconnecting: boolean
  laptopConnected: boolean
  /** The laptop hasn't connected after SLOW_LAPTOP_MS: still waiting, but say so. */
  laptopSlow: boolean
  /** The browser paused the microphone (screen locked, a call…): nothing is recorded until resumed. */
  micPaused: boolean
  /** Seconds of audio held on this phone, waiting for the connection or the laptop. */
  backlog: number
  /** Why an `unsaved` end wasn't confirmed as saved. */
  note?: string
  /** The laptop no longer knows this phone: the way on is to pair again. */
  unpaired?: boolean
  /** ms epoch the recording started on this phone. */
  startedAt?: number
  level: number
}

const MAX_SEGMENTS = 400

export class Meeting {
  view: MeetingView
  private relay: PhoneRelay | null = null
  private mic: MicCapture | null = null
  private sid = ''
  private timer: unknown = null
  private slowTimer: unknown = null
  /** Segments received in all: what to tell the laptop we have when asking it to resync. */
  private segmentCount = 0
  private source: AudioSource = 'mic'
  private listeners = new Set<(view: MeetingView) => void>()
  private finished = false

  private readonly pairing: Pairing
  private readonly deps: ListenDeps

  constructor(pairing: Pairing, deps: ListenDeps) {
    this.pairing = pairing
    this.deps = deps
    this.view = {
      status: 'connecting', laptopName: pairing.laptop_name, laptopState: '', segments: [], partial: '', speakers: {},
      reconnecting: false, laptopConnected: false, laptopSlow: false, micPaused: false, backlog: 0, level: 0,
    }
  }

  subscribe(fn: (view: MeetingView) => void): () => void {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }

  get sessionId(): string {
    return this.sid
  }

  /**
   * Start, from the tap itself: the microphone is asked for before anything is awaited,
   * because phones only start audio inside a gesture. Audio recorded before the laptop
   * says `ready` waits (up to 10 s) in the relay.
   */
  begin(source: AudioSource = 'mic'): Promise<void> {
    this.source = source
    return this.open(this.startCapture()).catch(() => this.fail('Something went wrong starting the recording. Try again.'))
  }

  private startCapture(): Promise<MicCapture> {
    const start = this.source === 'tab' && this.deps.startTab ? this.deps.startTab : this.deps.startMic
    return start(
      (pcm) => this.relay?.sendAudio(pcm),
      (level) => this.set({ level }),
      () => {
        if (!this.finished) this.set({ micPaused: true, level: 0 })
      },
    )
  }

  /**
   * After the browser paused the microphone: carry on with the same capture if it can,
   * or start a new one. Call from a tap (a new capture needs one on phones); it is also
   * tried when the page is shown again, where it works if the old capture can go on.
   * The session, its keys and the transcript so far are kept either way.
   */
  async resumeMic(fromTap = true): Promise<void> {
    if (this.finished || !this.view.micPaused || !this.mic) return
    if (await this.mic.resume()) {
      this.set({ micPaused: false })
      return
    }
    if (!fromTap) return
    this.stopMic()
    try {
      const mic = await this.startCapture()
      if (this.finished) {
        mic.stop()
        return
      }
      this.mic = mic
      this.set({ micPaused: false, error: undefined })
    } catch (e) {
      this.set({ error: e instanceof Error ? e.message : 'The microphone could not start again.' })
    }
  }

  private async open(micStarting: Promise<MicCapture>): Promise<void> {
    try {
      this.mic = await micStarting
    } catch (e) {
      this.fail(e instanceof Error ? e.message : 'The microphone could not start.')
      return
    }
    if (this.finished) {
      this.mic.stop()
      return
    }
    const phone = await this.deps.store.phoneKey()
    const eph = await newKeyPair()
    const hello: ListenHello = {
      phone_eph: await publicB64(eph.publicKey), phone_key: await publicB64(phone.publicKey), label: this.deps.label,
    }
    try {
      this.sid = await this.deps.openSession(this.pairing.runner_id, 'meeting', hello)
    } catch (e) {
      this.fail(openRefusal(e, this.pairing.laptop_name), false)
      return
    }
    if (this.finished) {
      void this.deps.endSession(this.sid).catch(() => undefined)
      return
    }
    const keys = await sessionKeysPhone(eph.privateKey, phone.privateKey, this.pairing.laptop_pub, this.sid)
    this.relay = new PhoneRelay(
      this.deps.wsUrl(`ws/listen/${this.sid}/?role=phone`),
      new Sealer(keys.up, this.sid),
      new Opener(keys.down, this.sid),
      {
        onControl: (m) => this.control(m),
        onPeer: (connected) => this.peer(connected),
        onEnded: (reason) => this.serverEnded(reason),
        onLost: (why) => this.fail(lostInWords(why)),
        onReconnecting: () => this.set({ reconnecting: true }),
        onResync: () => {
          this.set({ reconnecting: false })
          void this.relay?.sendControl({ type: 'resume', have: this.segmentCount })
        },
        onBacklog: (seconds) => {
          if (seconds !== this.view.backlog) this.set({ backlog: seconds })
        },
        onRefused: (reason) => {
          if (reason === 'unpaired') {
            this.set({ unpaired: true })
            this.fail(`${this.pairing.laptop_name} doesn't know this phone any more: it was unpaired there, or its key changed. Pair again.`)
          }
        },
      },
      this.deps.socket,
    )
    this.set({ status: 'waiting' })
    this.arm(LAPTOP_TIMEOUT_MS, () =>
      this.fail(this.view.laptopConnected
        ? `${this.pairing.laptop_name} didn't start listening. Check Canopy Listen is installed there.`
        : notAnswering(this.pairing.laptop_name)))
    this.slowTimer = this.deps.setTimeout(() => {
      if (!this.view.laptopConnected && this.view.status === 'waiting') this.set({ laptopSlow: true })
    }, SLOW_LAPTOP_MS)
    try {
      await this.relay.connect()
    } catch {
      this.fail('Could not connect to Canopy.')
    }
  }

  /** End and save. */
  end(): void {
    if (this.view.status !== 'recording' && this.view.status !== 'waiting') return
    this.stopMic()
    if (!this.relay?.ready) {
      this.discard()
      return
    }
    this.set({ status: 'ending' })
    void this.relay.sendControl({ type: 'end' })
    this.arm(SAVE_TIMEOUT_MS + this.view.backlog * 1000, () =>
      this.close({ status: 'unsaved', note: `${this.pairing.laptop_name} didn't say whether it saved the meeting. Check Meetings in Canopy, or Canopy Listen on the laptop.` }))
  }

  /** Throw it away: the laptop deletes what it has. */
  discard(): void {
    if (this.finished) return
    this.stopMic()
    const relay = this.relay
    if (relay?.ready) void relay.sendControl({ type: 'discard' })
    // Give the discard a moment to reach the laptop before the session closes.
    this.arm(relay?.ready ? 1500 : 0, () => this.close({ status: 'discarded' }))
    this.set({ status: 'ending' })
  }

  rename(speaker: number, name: string): void {
    const clean = name.trim().slice(0, 80)
    if (!clean || !this.relay?.ready) return
    this.set({ speakers: { ...this.view.speakers, [speaker]: clean } })
    void this.relay.sendControl({ type: 'rename', speaker, name: clean })
  }

  /** Leaving the page: stop everything without asking the laptop for anything. */
  dispose(): void {
    if (this.finished) return
    this.stopMic()
    this.close({}, false)
  }

  private control(m: Control): void {
    switch (m.type) {
      case 'ready':
        this.disarm()
        if (typeof m.name === 'string' && m.name) this.set({ laptopName: m.name })
        // The laptop's fresh nonce goes back sealed, so an old session's frames can't start one.
        void this.relay?.sendControl({
          type: 'start', notice_accepted: true, ...(typeof m.nonce === 'string' ? { nonce: m.nonce } : {}),
        })
        this.set({ status: 'recording', startedAt: Date.now(), laptopConnected: true, laptopSlow: false })
        return
      case 'state': {
        const state = typeof m.state === 'string' ? m.state : ''
        this.set({ laptopState: state })
        const detail = typeof m.detail === 'string' ? m.detail : ''
        if (state === 'failed') {
          this.fail(laptopFailure(detail, this.pairing.laptop_name))
        } else if (state === 'ended') {
          this.laptopEnded(detail)
        }
        return
      }
      case 'segment':
        this.addSegments(this.segmentCount, [m])
        return
      case 'segments':
        if (Array.isArray(m.items)) this.addSegments(Number.isInteger(m.from) ? (m.from as number) : this.segmentCount, m.items)
        return
      case 'partial':
        this.set({ partial: typeof m.text === 'string' ? m.text.slice(0, 2000) : '' })
        return
      case 'speakers': {
        if (!Array.isArray(m.speakers)) return
        const speakers: Record<number, string> = { ...this.view.speakers }
        for (const s of m.speakers as Array<Record<string, unknown>>) {
          if (Number.isInteger(s?.number) && typeof s.name === 'string') speakers[s.number as number] = s.name
        }
        this.set({ speakers })
        return
      }
      case 'chat':
        this.set({ chatUrl: safeUrl(m.url) })
        return
      case 'saved':
        this.close({ status: 'saved', savedUrl: safeUrl(m.url) })
        return
    }
  }

  private peer(connected: boolean): void {
    this.set({ laptopConnected: connected, reconnecting: false, laptopSlow: connected ? false : this.view.laptopSlow })
    // Connected but not yet ready: the laptop is starting Canopy Listen, which has its own limit.
    if (connected && this.view.status === 'waiting') {
      this.arm(READY_TIMEOUT_MS, () =>
        this.fail(`${this.pairing.laptop_name} didn't start listening. Check Canopy Listen is installed there.`))
    }
  }

  /** The laptop's recording is over: saved only if it says so. */
  private laptopEnded(detail: string): void {
    if (detail === 'not_saved') {
      this.close({ status: 'unsaved', note: `The recording ended on ${this.pairing.laptop_name}, but Voice assistant didn't save notes. The transcript is in Canopy Listen there.` })
    } else if (detail === 'discarded') {
      this.close({ status: 'discarded' })
    } else if (detail === 'saved') {
      this.arm(10_000, () => this.close({ status: 'saved' })) // the `saved` message, with its link, is on its way
    } else if (this.view.status === 'ending') {
      // A laptop that doesn't say how it ended: the old reading, saved once ended.
      this.arm(5000, () => this.close({ status: 'saved' }))
    } else {
      this.close({ status: 'unsaved', note: `${this.pairing.laptop_name} ended the recording.` })
    }
  }

  /** Segments `items`, the first being number `from` of the meeting; any already held are skipped. */
  private addSegments(from: number, items: unknown[]): void {
    const skip = Math.max(0, this.segmentCount - from)
    const fresh: Segment[] = []
    for (const raw of items.slice(skip)) {
      const m = (raw ?? {}) as Record<string, unknown>
      fresh.push({
        start: Number(m.start) || 0, end: Number(m.end) || 0,
        speaker: Number.isInteger(m.speaker) ? (m.speaker as number) : 0,
        text: typeof m.text === 'string' ? m.text : '',
      })
    }
    if (!fresh.length) return
    this.segmentCount = Math.max(this.segmentCount, from) + fresh.length
    const shown = fresh.filter((seg) => seg.text)
    if (shown.length) this.set({ segments: [...this.view.segments, ...shown].slice(-MAX_SEGMENTS) })
  }

  private serverEnded(reason: string): void {
    if (this.finished) return
    if (this.view.status === 'ending') {
      this.close({ status: 'unsaved', note: `The connection closed before ${this.pairing.laptop_name} said it saved the meeting. Check Meetings in Canopy.` }, false)
      return
    }
    const words: Record<string, string> = {
      peer_gone: `${this.pairing.laptop_name} lost its connection.`,
      not_connected: `${this.pairing.laptop_name} never connected.`,
      max_duration: 'Recordings stop after 4 hours.',
      ended_by_owner: 'You ended this session.',
    }
    this.fail(words[reason] ?? 'The session ended.', false)
  }

  private fail(message: string, endOnServer = true): void {
    if (this.finished) return
    this.stopMic()
    this.close({ status: 'failed', error: message }, endOnServer)
  }

  private close(patch: Partial<MeetingView>, endOnServer = true): void {
    if (this.finished) return
    this.finished = true
    this.disarm()
    if (this.slowTimer !== null) this.deps.clearTimeout(this.slowTimer)
    this.slowTimer = null
    this.stopMic()
    this.relay?.close()
    if (endOnServer && this.sid) void this.deps.endSession(this.sid).catch(() => undefined)
    this.set({ ...patch, reconnecting: false, laptopSlow: false, micPaused: false, backlog: 0, level: 0 })
  }

  private stopMic(): void {
    this.mic?.stop()
    this.mic = null
  }

  private arm(ms: number, fn: () => void): void {
    this.disarm()
    this.timer = this.deps.setTimeout(fn, ms)
  }

  private disarm(): void {
    if (this.timer !== null) this.deps.clearTimeout(this.timer)
    this.timer = null
  }

  private set(patch: Partial<MeetingView>): void {
    this.view = { ...this.view, ...patch }
    for (const fn of this.listeners) fn(this.view)
  }
}
