/**
 * The phone's end of one listen session's socket (/ws/listen/<id>/?role=phone).
 *
 * Seals what the phone sends, opens what the laptop sends, and keeps the protocol's
 * promises:
 *   - No audio before the laptop's `ready`.
 *   - Everything goes out through ONE ordered queue, sealed as it leaves. The laptop
 *     refuses a counter lower than one it has seen, so a frame sealed early and sent late
 *     would be thrown away.
 *   - A meeting outlives a dropped socket. The socket is reopened, with backoff, for up to
 *     RECONNECT_FOR_MS. While the socket is down, or the laptop is away, audio waits here
 *     (up to MAX_BACKLOG_CHUNKS, oldest dropped first) and is sent paced, under the relay's
 *     message rate, once both are back.
 *   - After any gap, `onResync` tells the page to ask the laptop for what it missed.
 * Text frames are the server's own notices (`peer`, `ended`).
 */
import { AUDIO, BadFrame, controlMessage, type Opener, type Sealer } from './relayCrypto'

export interface SocketLike {
  binaryType: string
  readonly readyState: number
  send(data: Uint8Array | ArrayBuffer): void
  close(code?: number): void
  onopen: ((ev: unknown) => void) | null
  onmessage: ((ev: { data: unknown }) => void) | null
  onclose: ((ev: { code: number }) => void) | null
  onerror: ((ev: unknown) => void) | null
}

export type SocketFactory = (url: string) => SocketLike

export const browserSocket: SocketFactory = (url) => new WebSocket(url) as unknown as SocketLike

export type Control = Record<string, unknown>

export interface RelayEvents {
  /** A control message from the laptop (after `ready` has been handled). */
  onControl(message: Control): void
  /** The laptop's side connected or left. */
  onPeer(connected: boolean): void
  /** The server ended the session. */
  onEnded(reason: string): void
  /** The socket is gone for good; `why` is a short code. */
  onLost(why: LostReason): void
  /** Reopening after a drop. */
  onReconnecting?(): void
  /** Back after a gap (socket reopened, or the laptop returned): ask for what was missed. */
  onResync?(): void
  /** Seconds of audio waiting to go out (0 when caught up). */
  onBacklog?(seconds: number): void
}

export type LostReason = 'dropped' | 'too_much' | 'taken' | 'refused' | 'signed_out'

/** Half-second chunks: 360 is 3 minutes, the server's limit on a side being away. */
export const MAX_BACKLOG_CHUNKS = 360
/** How long to keep reopening a dropped socket: under the server's 3-minute limit. */
export const RECONNECT_FOR_MS = 170_000
/** Frames per second when catching up: under the relay's 60 per second per side. */
export const DRAIN_PER_SECOND = 30
const OPEN = 1
const FATAL: Record<number, LostReason> = { 4001: 'signed_out', 4003: 'refused', 4008: 'too_much', 4009: 'taken' }

type Item = { audio: Uint8Array } | { control: Control; done: () => void }

export class PhoneRelay {
  ready = false
  private socket: SocketLike | null = null
  private closing = false
  private ended = false
  private laptopHere = false
  private droppedAt = 0
  private everOpened = false
  private queue: Item[] = []
  private audioQueued = 0
  private pumping = false
  private sentThisSecond = 0
  private secondStarted = 0

  private readonly url: string
  private readonly sealer: Sealer
  private readonly opener: Opener
  private readonly events: RelayEvents
  private readonly makeSocket: SocketFactory
  private readonly now: () => number
  private readonly reconnectDelayMs: number
  private readonly wait: (ms: number) => Promise<void>

  constructor(
    url: string,
    sealer: Sealer,
    opener: Opener,
    events: RelayEvents,
    makeSocket: SocketFactory = browserSocket,
    now: () => number = Date.now,
    reconnectDelayMs = 1000,
    wait: (ms: number) => Promise<void> = (ms) => new Promise((r) => setTimeout(r, ms)),
  ) {
    this.url = url
    this.sealer = sealer
    this.opener = opener
    this.events = events
    this.makeSocket = makeSocket
    this.now = now
    this.reconnectDelayMs = reconnectDelayMs
    this.wait = wait
  }

  /** Resolves when the socket opens, rejects if the first attempt fails. */
  connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      let opened = false
      const ws = this.makeSocket(this.url)
      ws.binaryType = 'arraybuffer'
      this.socket = ws
      ws.onopen = () => {
        opened = true
        const back = this.everOpened
        this.everOpened = true
        this.droppedAt = 0
        resolve()
        if (back && this.ready) this.events.onResync?.()
        void this.pump()
      }
      ws.onmessage = (ev) => void this.receive(ev.data)
      ws.onerror = () => undefined
      ws.onclose = (ev) => {
        if (this.socket === ws) this.socket = null
        if (!opened) {
          reject(new Error(FATAL[ev.code] ?? 'dropped'))
          if (this.everOpened) this.retry(ev.code)
          return
        }
        this.retry(ev.code)
      }
    })
  }

  /**
   * A control message, sent whatever `ready` says (start, pair, rename…). It goes ahead of
   * audio still waiting, so `start` precedes the audio held from before `ready` — except
   * `end`, which follows it, so the last words are in the recording.
   */
  sendControl(message: Control): Promise<void> {
    return new Promise((resolve) => {
      const item: Item = { control: message, done: resolve }
      const firstAudio = this.queue.findIndex((i) => 'audio' in i)
      if (message.type === 'end' || firstAudio < 0) this.queue.push(item)
      else this.queue.splice(firstAudio, 0, item)
      void this.pump()
    })
  }

  /** Audio: held until `ready`, and while there is no socket or no laptop; bounded. */
  sendAudio(pcm: Uint8Array): void {
    this.queue.push({ audio: pcm })
    this.audioQueued += 1
    while (this.audioQueued > MAX_BACKLOG_CHUNKS) {
      const i = this.queue.findIndex((item) => 'audio' in item)
      this.queue.splice(i, 1)
      this.audioQueued -= 1
    }
    void this.pump()
  }

  /** Close on purpose: no reconnect, no `onLost`. */
  close(): void {
    this.closing = true
    this.socket?.close(1000)
    this.socket = null
  }

  /** Seconds of audio not yet sent. */
  get backlogSeconds(): number {
    return this.audioQueued / 2
  }

  private canSend(item: Item): boolean {
    if (!this.socket || this.socket.readyState !== OPEN) return false
    if ('audio' in item) return this.ready && this.laptopHere
    return true
  }

  /** Send what can go, in order: audio waits for the laptop, and so does what's behind it. */
  private async pump(): Promise<void> {
    if (this.pumping) return
    this.pumping = true
    try {
      while (this.queue.length && this.canSend(this.queue[0]) && !this.closing) {
        if (this.audioQueued > 2) await this.pace()
        if (!this.queue.length || !this.canSend(this.queue[0])) break
        const item = this.queue.shift()!
        const frame = 'audio' in item ? await this.sealer.seal(AUDIO, item.audio) : await this.sealer.control(item.control)
        const socket = this.socket
        if (!socket || socket.readyState !== OPEN) {
          // Dropped while sealing: put it back. Its counter is spent, so it's sealed again
          // with a new one, and the laptop accepts the gap.
          this.queue.unshift(item)
          break
        }
        socket.send(frame)
        if ('audio' in item) this.audioQueued -= 1
        else item.done()
      }
    } finally {
      this.pumping = false
      this.events.onBacklog?.(this.backlogSeconds)
    }
  }

  /** Catching up: at most DRAIN_PER_SECOND frames a second. */
  private async pace(): Promise<void> {
    const now = this.now()
    if (now - this.secondStarted >= 1000) {
      this.secondStarted = now
      this.sentThisSecond = 0
    }
    this.sentThisSecond += 1
    if (this.sentThisSecond > DRAIN_PER_SECOND) {
      await this.wait(1000 - (now - this.secondStarted))
      this.secondStarted = this.now()
      this.sentThisSecond = 1
    }
  }

  private async receive(data: unknown): Promise<void> {
    if (typeof data === 'string') {
      let note: Control | null = null
      try {
        note = JSON.parse(data) as Control
      } catch {
        return
      }
      if (note?.type === 'peer' && note.role === 'laptop') {
        const here = note.connected === true
        const back = here && !this.laptopHere && this.ready
        this.laptopHere = here
        this.events.onPeer(here)
        if (back) this.events.onResync?.()
        if (here) void this.pump()
      }
      if (note?.type === 'ended') {
        this.ended = true
        this.events.onEnded(typeof note.reason === 'string' ? note.reason : 'ended')
      }
      return
    }
    const bytes = data instanceof ArrayBuffer ? new Uint8Array(data) : data instanceof Uint8Array ? data : null
    if (!bytes) return
    let message: Control | null
    try {
      message = controlMessage(await this.opener.open(bytes))
    } catch (e) {
      if (e instanceof BadFrame) return // forged or replayed: dropped, never shown
      throw e
    }
    if (!message) return
    if (message.type === 'ready' && !this.ready) {
      this.ready = true
      this.laptopHere = true // it just spoke
      this.events.onControl(message)
      void this.pump()
      return
    }
    this.events.onControl(message)
  }

  private retry(code: number): void {
    if (this.closing || this.ended) return
    const fatal = FATAL[code]
    if (fatal) {
      this.lost(fatal)
      return
    }
    if (!this.droppedAt) this.droppedAt = this.now()
    if (this.now() - this.droppedAt > RECONNECT_FOR_MS) {
      this.lost('dropped')
      return
    }
    this.events.onReconnecting?.()
    this.events.onBacklog?.(this.backlogSeconds)
    const tries = Math.max(0, Math.round((this.now() - this.droppedAt) / this.reconnectDelayMs))
    const delay = Math.min(this.reconnectDelayMs * 2 ** Math.min(tries, 4), 10 * this.reconnectDelayMs)
    setTimeout(() => {
      if (this.closing || this.ended) return
      this.connect().catch(() => undefined)
    }, delay)
  }

  private lost(why: LostReason): void {
    if (this.closing || this.ended) return
    this.closing = true
    this.events.onLost(why)
  }
}
