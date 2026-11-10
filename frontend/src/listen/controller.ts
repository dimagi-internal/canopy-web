/**
 * The two things the phone does over the relay — pair once, then record meetings — as
 * plain classes the page drives. Everything outside (REST, the socket, storage, the
 * microphone) comes in through `ListenDeps`, so the flows test without a browser.
 */
import { ApiError } from '@/api/problem'
import type { ListenHello, ListenKind, ListenRunner } from '@/api/listen'
import type { MicCapture } from './audio'
import type { PairInvite } from './pairLink'
import { isExpired } from './pairLink'
import { PhoneRelay, type Control, type LostReason, type SocketFactory } from './relay'
import { newKeyPair, Opener, pairKeys, publicB64, Sealer, sessionKeysPhone } from './relayCrypto'
import type { ListenStore, Pairing } from './store'

export interface ListenDeps {
  openSession(runnerId: string, kind: ListenKind, hello: ListenHello): Promise<string>
  endSession(id: string): Promise<unknown>
  listRunners(): Promise<ListenRunner[]>
  store: ListenStore
  socket: SocketFactory
  wsUrl(path: string): string
  label: string
  startMic(onChunk: (pcm: Uint8Array) => void, onLevel: (level: number) => void): Promise<MicCapture>
  /** Timers, injectable for tests. */
  setTimeout(fn: () => void, ms: number): unknown
  clearTimeout(handle: unknown): void
}

/** How long the laptop has to answer before the phone gives up. */
export const READY_TIMEOUT_MS = 45_000
export const PAIR_TIMEOUT_MS = 60_000
export const SAVE_TIMEOUT_MS = 90_000

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

// -- pairing -------------------------------------------------------------------------

export type PairStep = 'connecting' | 'waiting' | 'done'

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

// -- a meeting -----------------------------------------------------------------------

export interface Segment {
  start: number
  end: number
  speaker: number
  text: string
}

export type MeetingStatus = 'connecting' | 'waiting' | 'recording' | 'ending' | 'saved' | 'discarded' | 'failed'

export interface MeetingView {
  status: MeetingStatus
  laptopName: string
  /** The laptop's own word for where it is (starting, recording, ending…). */
  laptopState: string
  segments: Segment[]
  speakers: Record<number, string>
  chatUrl?: string
  savedUrl?: string
  error?: string
  reconnecting: boolean
  laptopConnected: boolean
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
  private listeners = new Set<(view: MeetingView) => void>()
  private finished = false

  private readonly pairing: Pairing
  private readonly deps: ListenDeps

  constructor(pairing: Pairing, deps: ListenDeps) {
    this.pairing = pairing
    this.deps = deps
    this.view = {
      status: 'connecting', laptopName: pairing.laptop_name, laptopState: '', segments: [], speakers: {},
      reconnecting: false, laptopConnected: false, level: 0,
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
  begin(): Promise<void> {
    const micStarting = this.deps.startMic(
      (pcm) => this.relay?.sendAudio(pcm),
      (level) => this.set({ level }),
    )
    return this.open(micStarting).catch(() => this.fail('Something went wrong starting the recording. Try again.'))
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
        onPeer: (connected) => this.set({ laptopConnected: connected, reconnecting: false }),
        onEnded: (reason) => this.serverEnded(reason),
        onLost: (why) => this.fail(lostInWords(why)),
        onReconnecting: () => this.set({ reconnecting: true }),
      },
      this.deps.socket,
    )
    this.set({ status: 'waiting' })
    this.arm(READY_TIMEOUT_MS, () => this.fail(`${this.pairing.laptop_name} didn't start listening. Check Canopy Listen is installed there.`))
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
    this.arm(SAVE_TIMEOUT_MS, () => this.close({ status: 'saved' }))
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
        void this.relay?.sendControl({ type: 'start', notice_accepted: true })
        this.set({ status: 'recording', startedAt: Date.now() })
        return
      case 'state': {
        const state = typeof m.state === 'string' ? m.state : ''
        this.set({ laptopState: state })
        if (state === 'failed') {
          const detail = typeof m.detail === 'string' && m.detail ? m.detail : 'The laptop stopped the recording.'
          this.fail(detail)
        } else if (state === 'ended' && this.view.status === 'ending') {
          this.arm(5000, () => this.close({ status: 'saved' }))
        }
        return
      }
      case 'segment': {
        const seg: Segment = {
          start: Number(m.start) || 0, end: Number(m.end) || 0,
          speaker: Number.isInteger(m.speaker) ? (m.speaker as number) : 0,
          text: typeof m.text === 'string' ? m.text : '',
        }
        if (!seg.text) return
        this.set({ segments: [...this.view.segments, seg].slice(-MAX_SEGMENTS) })
        return
      }
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

  private serverEnded(reason: string): void {
    if (this.finished) return
    if (this.view.status === 'ending') {
      this.close({ status: 'saved' }, false)
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
    this.stopMic()
    this.relay?.close()
    if (endOnServer && this.sid) void this.deps.endSession(this.sid).catch(() => undefined)
    this.set({ ...patch, reconnecting: false, level: 0 })
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
