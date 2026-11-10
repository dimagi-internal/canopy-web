/**
 * The phone's end of one listen session's socket (/ws/listen/<id>/?role=phone).
 *
 * Seals what the phone sends, opens what the laptop sends, and keeps the protocol's
 * promises: no audio before the laptop's `ready` (up to 10 s of it waits here, oldest
 * dropped first), frames go out in counter order, and a dropped socket is reopened once
 * before giving up. Text frames are the server's own notices (`peer`, `ended`).
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
}

export type LostReason = 'dropped' | 'too_much' | 'taken' | 'refused' | 'signed_out'

/** Half-second chunks: 20 is 10 s. */
export const MAX_BUFFERED_AUDIO = 20
const MAX_OUTBOX = 40
const OPEN = 1
const FATAL: Record<number, LostReason> = { 4001: 'signed_out', 4003: 'refused', 4008: 'too_much', 4009: 'taken' }

export class PhoneRelay {
  ready = false
  private socket: SocketLike | null = null
  private closing = false
  private ended = false
  private lastReconnect = 0
  private beforeReady: Uint8Array[] = []
  private outbox: Uint8Array[] = []

  private readonly url: string
  private readonly sealer: Sealer
  private readonly opener: Opener
  private readonly events: RelayEvents
  private readonly makeSocket: SocketFactory
  private readonly now: () => number
  private readonly reconnectDelayMs: number

  constructor(
    url: string,
    sealer: Sealer,
    opener: Opener,
    events: RelayEvents,
    makeSocket: SocketFactory = browserSocket,
    now: () => number = Date.now,
    reconnectDelayMs = 1000,
  ) {
    this.url = url
    this.sealer = sealer
    this.opener = opener
    this.events = events
    this.makeSocket = makeSocket
    this.now = now
    this.reconnectDelayMs = reconnectDelayMs
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
        this.drainOutbox()
        resolve()
      }
      ws.onmessage = (ev) => void this.receive(ev.data)
      ws.onerror = () => undefined
      ws.onclose = (ev) => {
        if (this.socket === ws) this.socket = null
        if (!opened) {
          reject(new Error(FATAL[ev.code] ?? 'dropped'))
          if (this.lastReconnect) this.lost(FATAL[ev.code] ?? 'dropped')
          return
        }
        this.dropped(ev.code)
      }
    })
  }

  /** A control message, sent whatever `ready` says (start, end, pair…). */
  sendControl(message: Control): Promise<void> {
    return this.sealer.control(message).then((frame) => this.post(frame))
  }

  /** Audio: held until `ready`, at most 10 s of it. */
  sendAudio(pcm: Uint8Array): void {
    if (!this.ready) {
      this.beforeReady.push(pcm)
      if (this.beforeReady.length > MAX_BUFFERED_AUDIO) this.beforeReady.shift()
      return
    }
    void this.sealer.seal(AUDIO, pcm).then((frame) => this.post(frame))
  }

  /** Close on purpose: no reconnect, no `onLost`. */
  close(): void {
    this.closing = true
    this.socket?.close(1000)
    this.socket = null
  }

  private post(frame: Uint8Array): void {
    if (this.socket && this.socket.readyState === OPEN) {
      this.socket.send(frame)
      return
    }
    this.outbox.push(frame)
    if (this.outbox.length > MAX_OUTBOX) this.outbox.shift()
  }

  private drainOutbox(): void {
    const pending = this.outbox
    this.outbox = []
    for (const frame of pending) this.post(frame)
  }

  private async receive(data: unknown): Promise<void> {
    if (typeof data === 'string') {
      let note: Control | null = null
      try {
        note = JSON.parse(data) as Control
      } catch {
        return
      }
      if (note?.type === 'peer' && note.role === 'laptop') this.events.onPeer(note.connected === true)
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
      const held = this.beforeReady
      this.beforeReady = []
      this.events.onControl(message)
      for (const pcm of held) this.sendAudio(pcm)
      return
    }
    this.events.onControl(message)
  }

  private dropped(code: number): void {
    if (this.closing || this.ended) return
    const fatal = FATAL[code]
    if (fatal) {
      this.lost(fatal)
      return
    }
    // Once: a second drop inside 30 s of the last reopen is the end of it.
    if (this.lastReconnect && this.now() - this.lastReconnect < 30_000) {
      this.lost('dropped')
      return
    }
    this.lastReconnect = this.now()
    this.events.onReconnecting?.()
    setTimeout(() => {
      if (this.closing || this.ended) return
      this.connect().catch(() => undefined)
    }, this.reconnectDelayMs)
  }

  private lost(why: LostReason): void {
    if (this.closing || this.ended) return
    this.closing = true
    this.events.onLost(why)
  }
}
