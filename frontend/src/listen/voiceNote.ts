/**
 * A voice note: record on this phone, have the paired laptop read it (Canopy Listen, its
 * own speech model, nothing leaves it but the words), and get the words back. The same
 * end-to-end relay as a meeting, but no meeting: nothing is recorded or saved on the
 * laptop, and nothing is sent to an agent until the page does it, as this person.
 */
import type { ListenHello } from '@/api/listen'
import type { MicCapture } from './audio'
import { LAPTOP_TIMEOUT_MS, READY_TIMEOUT_MS, SLOW_LAPTOP_MS, lostInWords, notAnswering, openRefusal, type ListenDeps } from './controller'
import { PhoneRelay, type Control } from './relay'
import { newKeyPair, Opener, publicB64, Sealer, sessionKeysPhone } from './relayCrypto'
import type { Pairing } from './store'

/** The laptop cuts a note here too. */
export const NOTE_MAX_MS = 600_000
/** Reading a note: the first time includes the speech model's download. */
export const TRANSCRIBE_TIMEOUT_MS = 330_000

export type NoteStatus = 'connecting' | 'waiting' | 'recording' | 'transcribing' | 'done' | 'discarded' | 'failed'

export interface NoteView {
  status: NoteStatus
  laptopName: string
  /** The words, once the laptop has read them. */
  text: string
  error?: string
  /** The laptop no longer knows this phone: pair again. */
  unpaired?: boolean
  laptopSlow: boolean
  reconnecting: boolean
  micPaused: boolean
  /** ms epoch the recording started. */
  startedAt?: number
  level: number
}

const NOTE_FAILURES: Record<string, (name: string) => string> = {
  no_words: () => "No words were heard. Check the microphone and try again.",
  transcribe_failed: (n) => `${n} couldn't read the note. Check Canopy Listen there, then try again.`,
  transcribe_timeout: (n) => `${n} took too long reading the note. Try a shorter one.`,
  update_page: (n) => `Reload this page: Canopy Listen on ${n} needs its newest version.`,
}

export class VoiceNote {
  view: NoteView
  private relay: PhoneRelay | null = null
  private mic: MicCapture | null = null
  private sid = ''
  private timer: unknown = null
  private slowTimer: unknown = null
  private limitTimer: unknown = null
  private finished = false
  private listeners = new Set<(view: NoteView) => void>()
  private readonly pairing: Pairing
  private readonly deps: ListenDeps

  constructor(pairing: Pairing, deps: ListenDeps) {
    this.pairing = pairing
    this.deps = deps
    this.view = {
      status: 'connecting', laptopName: pairing.laptop_name, text: '', laptopSlow: false, reconnecting: false,
      micPaused: false, level: 0,
    }
  }

  subscribe(fn: (view: NoteView) => void): () => void {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }

  /** Start, from the tap itself: phones only start the microphone inside a gesture. */
  begin(): Promise<void> {
    const mic = this.deps.startMic(
      (pcm) => this.relay?.sendAudio(pcm),
      (level) => this.set({ level }),
      () => {
        if (!this.finished) this.set({ micPaused: true, level: 0 })
      },
    )
    return this.open(mic).catch(() => this.fail('Something went wrong starting the recording. Try again.'))
  }

  /** Done talking: the laptop reads what it heard. */
  stop(): void {
    const { status } = this.view
    if (status !== 'recording' && status !== 'waiting' && status !== 'connecting') return
    this.stopMic()
    if (this.limitTimer !== null) this.deps.clearTimeout(this.limitTimer)
    this.limitTimer = null
    const waiting = status !== 'recording'
    this.set({ status: 'transcribing', level: 0, micPaused: false })
    // Stopped before the laptop answered: what was said is held here, and goes with the
    // end once it does (the laptop's own timeouts still apply until then).
    if (!waiting) this.askToRead()
  }

  private askToRead(): void {
    void this.relay?.sendControl({ type: 'end' })
    this.arm(TRANSCRIBE_TIMEOUT_MS, () => this.fail(NOTE_FAILURES.transcribe_timeout(this.pairing.laptop_name)))
  }

  /** Throw it away: nothing is read, nothing is sent. */
  discard(): void {
    if (this.finished) return
    this.stopMic()
    const relay = this.relay
    if (relay?.ready) void relay.sendControl({ type: 'discard' })
    this.arm(relay?.ready ? 1000 : 0, () => this.close({ status: 'discarded' }))
  }

  /** Leaving the page. */
  dispose(): void {
    if (this.finished) return
    this.close({}, true)
  }

  private async open(micStarting: Promise<MicCapture>): Promise<void> {
    try {
      this.mic = await micStarting
    } catch (e) {
      this.fail(e instanceof Error ? e.message : 'The microphone could not start.')
      return
    }
    if (this.finished) {
      this.stopMic()
      return
    }
    this.set({ startedAt: Date.now() })
    this.limitTimer = this.deps.setTimeout(() => this.stop(), NOTE_MAX_MS)
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
        onEnded: (reason) => this.fail(reason === 'peer_gone' ? `${this.pairing.laptop_name} lost its connection.` : 'The session ended.', false),
        onLost: (why) => this.fail(lostInWords(why)),
        onReconnecting: () => this.set({ reconnecting: true }),
        onResync: () => {
          this.set({ reconnecting: false })
          void this.relay?.sendControl({ type: 'resume', have: 0 })
        },
        onRefused: (reason) => {
          if (reason === 'unpaired') {
            this.set({ unpaired: true })
            this.fail(`${this.pairing.laptop_name} doesn't know this phone any more. Pair again.`)
          }
        },
      },
      this.deps.socket,
    )
    if (this.view.status === 'connecting') this.set({ status: 'waiting' })
    this.arm(LAPTOP_TIMEOUT_MS, () => this.fail(notAnswering(this.pairing.laptop_name)))
    this.slowTimer = this.deps.setTimeout(() => {
      if (!this.relay?.ready && !this.finished) this.set({ laptopSlow: true })
    }, SLOW_LAPTOP_MS)
    try {
      await this.relay.connect()
    } catch {
      this.fail('Could not connect to Canopy.')
    }
  }

  private peer(connected: boolean): void {
    this.set({ reconnecting: false, laptopSlow: connected ? false : this.view.laptopSlow })
    if (connected && !this.relay?.ready && !this.finished) {
      this.arm(READY_TIMEOUT_MS, () =>
        this.fail(`${this.pairing.laptop_name} didn't start listening. Check Canopy Listen is installed there.`))
    }
  }

  private control(m: Control): void {
    switch (m.type) {
      case 'ready': {
        // An older Mac would take a note's start for a meeting's: ask only one that says it can.
        const modes = Array.isArray(m.modes) ? m.modes : []
        if (!modes.includes('note')) {
          this.fail(NOTE_FAILURES.update_page(this.pairing.laptop_name))
          return
        }
        if (typeof m.name === 'string' && m.name) this.set({ laptopName: m.name })
        void this.relay?.sendControl({ type: 'start', mode: 'note', nonce: m.nonce })
        this.disarm()
        this.set({ laptopSlow: false })
        // Still talking: stop() takes it from here. Already stopped while waiting: ask now.
        if (this.view.status === 'waiting') this.set({ status: 'recording' })
        else if (this.view.status === 'transcribing') this.askToRead()
        return
      }
      case 'note':
        if (typeof m.text === 'string') this.set({ text: m.text.slice(0, 20_000) })
        return
      case 'state': {
        const state = typeof m.state === 'string' ? m.state : ''
        const detail = typeof m.detail === 'string' ? m.detail : ''
        if (state === 'failed') {
          const words = NOTE_FAILURES[detail]
          this.fail(words ? words(this.pairing.laptop_name) : detail || `${this.pairing.laptop_name} couldn't read the note.`)
        } else if (state === 'ended' && detail === 'transcribed' && this.view.text) {
          this.close({ status: 'done' })
        } else if (state === 'ended' && detail === 'discarded') {
          this.close({ status: 'discarded' })
        }
        return
      }
    }
  }

  private fail(message: string, endOnServer = true): void {
    if (this.finished) return
    this.close({ status: 'failed', error: message }, endOnServer)
  }

  private close(patch: Partial<NoteView>, endOnServer = true): void {
    if (this.finished) return
    this.finished = true
    this.disarm()
    for (const t of [this.slowTimer, this.limitTimer]) if (t !== null) this.deps.clearTimeout(t)
    this.slowTimer = this.limitTimer = null
    this.stopMic()
    this.relay?.close()
    if (endOnServer && this.sid) void this.deps.endSession(this.sid).catch(() => undefined)
    this.set({ ...patch, reconnecting: false, laptopSlow: false, micPaused: false, level: 0 })
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

  private set(patch: Partial<NoteView>): void {
    this.view = { ...this.view, ...patch }
    for (const fn of this.listeners) fn(this.view)
  }
}
