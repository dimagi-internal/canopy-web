/**
 * The phone's microphone as 16 kHz mono int16 little-endian PCM, in ~0.5 s chunks.
 *
 * getUserMedia (echo cancellation, noise suppression) → an AudioWorklet tap at the
 * device's own rate → `Downsampler` (a box filter that averages each output sample's
 * span of input, so it never aliases and works for any rate ≥ 16 kHz) → `PcmChunker`.
 */

export const TARGET_RATE = 16000
/** Samples per chunk: half a second. */
export const CHUNK_SAMPLES = TARGET_RATE / 2

/** Any input rate down to `outRate`, by averaging the input each output sample covers. */
export class Downsampler {
  private readonly step: number
  private sum = 0
  private weight = 0

  constructor(inRate: number, outRate = TARGET_RATE) {
    if (inRate < outRate) throw new Error(`the microphone runs at ${inRate} Hz, below ${outRate}`)
    this.step = inRate / outRate
  }

  push(input: Float32Array): Float32Array {
    const out: number[] = []
    for (const sample of input) {
      let left = 1 // how much of this input sample is still to be counted
      while (left > 0) {
        const take = Math.min(left, this.step - this.weight)
        this.sum += sample * take
        this.weight += take
        left -= take
        if (this.weight >= this.step - 1e-9) {
          out.push(this.sum / this.weight)
          this.sum = 0
          this.weight = 0
        }
      }
    }
    return Float32Array.from(out)
  }
}

/** Float samples to int16 little-endian, cut into chunks of CHUNK_SAMPLES. */
export class PcmChunker {
  private pending: number[] = []
  private readonly size: number

  constructor(size = CHUNK_SAMPLES) {
    this.size = size
  }

  push(samples: Float32Array): Uint8Array[] {
    for (const s of samples) this.pending.push(s)
    const chunks: Uint8Array[] = []
    while (this.pending.length >= this.size) chunks.push(toInt16LE(this.pending.splice(0, this.size)))
    return chunks
  }

  /** What is left, as one short chunk (or null). */
  flush(): Uint8Array | null {
    if (!this.pending.length) return null
    return toInt16LE(this.pending.splice(0))
  }
}

export function toInt16LE(samples: ArrayLike<number>): Uint8Array {
  const out = new Uint8Array(samples.length * 2)
  const view = new DataView(out.buffer)
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]))
    view.setInt16(i * 2, s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7fff), true)
  }
  return out
}

/** Root mean square of a block, 0..1. */
export function rms(block: Float32Array): number {
  if (!block.length) return 0
  let sum = 0
  for (const s of block) sum += s * s
  return Math.sqrt(sum / block.length)
}

const WORKLET = `
class CanopyListenTap extends AudioWorkletProcessor {
  constructor() { super(); this.buf = new Float32Array(2048); this.n = 0 }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0]
    if (ch) {
      for (let i = 0; i < ch.length; i++) {
        this.buf[this.n++] = ch[i]
        if (this.n === this.buf.length) { this.port.postMessage(this.buf.slice(0)); this.n = 0 }
      }
    }
    return true
  }
}
registerProcessor('canopy-listen-tap', CanopyListenTap)
`

export class MicDenied extends Error {
  readonly why: 'denied' | 'missing' | 'unsupported' | 'other'

  constructor(why: 'denied' | 'missing' | 'unsupported' | 'other', message: string) {
    super(message)
    this.name = 'MicDenied'
    this.why = why
  }
}

export interface MicCapture {
  stop(): void
  /**
   * Try to carry on after a pause (see `onPause`). True if sound is flowing again; false
   * means this capture is finished and a new one has to be started from a tap.
   */
  resume(): Promise<boolean>
}

/**
 * Called once each time the browser stops the sound under a running capture: the screen
 * locked, a phone call came in, another app took the microphone, or a shared tab stopped.
 * Phones do this without asking, and a recording that just goes quiet looks like it's
 * still running, so the page has to be told.
 */
export type OnPause = () => void

/**
 * Start the microphone. Call from the tap itself: phones only let audio start in a
 * gesture. `onChunk` gets each 0.5 s of PCM; `onLevel` a smoothed 0..1 level.
 */
export async function startMic(
  onChunk: (pcm: Uint8Array) => void,
  onLevel: (level: number) => void,
  onPause?: OnPause,
): Promise<MicCapture> {
  if (!navigator.mediaDevices?.getUserMedia || typeof AudioWorkletNode === 'undefined') {
    throw new MicDenied('unsupported', 'This browser cannot record audio here.')
  }
  const Ctx = window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
  const ctx = new Ctx()
  const resumed = ctx.resume()
  let stream: MediaStream
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    })
  } catch (e) {
    void ctx.close()
    const name = (e as { name?: string })?.name
    if (name === 'NotAllowedError' || name === 'SecurityError') {
      throw new MicDenied('denied', 'Microphone access is turned off for this site.')
    }
    if (name === 'NotFoundError' || name === 'OverconstrainedError') {
      throw new MicDenied('missing', 'No microphone was found.')
    }
    throw new MicDenied('other', 'The microphone could not start.')
  }
  return tapStream(ctx, resumed, [stream], onChunk, onLevel, onPause)
}

/** Run `streams`, mixed, through the tap: 16 kHz int16 chunks to `onChunk`, a level to `onLevel`. */
async function tapStream(
  ctx: AudioContext,
  resumed: Promise<void>,
  streams: MediaStream[],
  onChunk: (pcm: Uint8Array) => void,
  onLevel: (level: number) => void,
  onPause?: OnPause,
): Promise<MicCapture> {
  const url = URL.createObjectURL(new Blob([WORKLET], { type: 'text/javascript' }))
  try {
    await ctx.audioWorklet.addModule(url)
  } finally {
    URL.revokeObjectURL(url)
  }
  await resumed
  const sources = streams.map((stream) => ctx.createMediaStreamSource(stream))
  const tap = new AudioWorkletNode(ctx, 'canopy-listen-tap', { numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1 })
  const silent = ctx.createGain()
  silent.gain.value = 0
  for (const source of sources) source.connect(tap) // a node's inputs sum: the streams mix
  tap.connect(silent)
  silent.connect(ctx.destination) // keeps the graph pulling on every browser
  const down = new Downsampler(ctx.sampleRate)
  const chunker = new PcmChunker()
  let level = 0
  tap.port.onmessage = (ev: MessageEvent<Float32Array>) => {
    level = Math.max(rms(ev.data) * 3, level * 0.85)
    onLevel(Math.min(1, level))
    for (const chunk of chunker.push(down.push(ev.data))) onChunk(chunk)
  }
  let stopped = false
  let paused = false
  // The first stream is the one that matters: the microphone, or the shared tab.
  const main = streams[0].getAudioTracks()
  const pause = () => {
    if (stopped || paused) return
    paused = true
    onPause?.()
  }
  for (const track of main) {
    track.addEventListener('ended', pause)
    track.addEventListener('mute', pause)
  }
  ctx.onstatechange = () => {
    if (ctx.state !== 'running') pause()
  }
  if (ctx.state !== 'running' || main.some((t) => t.muted)) queueMicrotask(pause)
  return {
    async resume() {
      if (stopped) return false
      if (main.some((t) => t.readyState === 'ended')) return false
      try {
        await ctx.resume()
      } catch {
        return false
      }
      const flowing = ctx.state === 'running' && main.every((t) => !t.muted)
      if (flowing) paused = false
      return flowing
    },
    stop() {
      if (stopped) return
      stopped = true
      ctx.onstatechange = null
      for (const track of main) {
        track.removeEventListener('ended', pause)
        track.removeEventListener('mute', pause)
      }
      tap.port.onmessage = null
      const rest = chunker.flush()
      if (rest) onChunk(rest)
      for (const stream of streams) for (const track of stream.getTracks()) track.stop()
      for (const source of sources) source.disconnect()
      tap.disconnect()
      void ctx.close()
    },
  }
}

/** Whether this browser can share a tab's audio (desktop Chrome and Edge can; phones can't). */
export function canCaptureTab(): boolean {
  return typeof navigator !== 'undefined' && typeof navigator.mediaDevices?.getDisplayMedia === 'function'
    && !/Android|iPhone|iPad|iPod/i.test(navigator.userAgent)
}

/**
 * Record a call in another browser tab (Meet, Zoom on the web): the tab's sound plus this
 * computer's microphone, mixed. Call from a click. The browser asks which tab; the person
 * must tick "Also share tab audio". The picture is stopped at once: only sound is used.
 */
export async function startTab(
  onChunk: (pcm: Uint8Array) => void,
  onLevel: (level: number) => void,
  onPause?: OnPause,
): Promise<MicCapture> {
  if (!canCaptureTab() || typeof AudioWorkletNode === 'undefined') {
    throw new MicDenied('unsupported', 'This browser cannot share a tab’s audio. Use Chrome or Edge on a computer.')
  }
  const ctx = new AudioContext()
  const resumed = ctx.resume()
  let stream: MediaStream
  try {
    stream = await navigator.mediaDevices.getDisplayMedia({
      video: true,
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      preferCurrentTab: false,
      selfBrowserSurface: 'exclude',
      systemAudio: 'include',
    } as DisplayMediaStreamOptions)
  } catch (e) {
    void ctx.close()
    const name = (e as { name?: string })?.name
    if (name === 'NotAllowedError') throw new MicDenied('denied', 'Sharing was cancelled.')
    throw new MicDenied('other', 'The tab could not be shared.')
  }
  for (const track of stream.getVideoTracks()) track.stop()
  if (stream.getAudioTracks().length === 0) {
    void ctx.close()
    throw new MicDenied('missing', 'That share had no sound. Share again and tick “Also share tab audio”.')
  }
  // The tab carries everyone else on the call; this computer's microphone carries you.
  const streams = [stream]
  try {
    streams.push(await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    }))
  } catch {
    // No microphone, or refused: the tab alone still records the others.
  }
  return tapStream(ctx, resumed, streams, onChunk, onLevel, onPause)
}
