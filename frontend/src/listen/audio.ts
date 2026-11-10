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
}

/**
 * Start the microphone. Call from the tap itself: phones only let audio start in a
 * gesture. `onChunk` gets each 0.5 s of PCM; `onLevel` a smoothed 0..1 level.
 */
export async function startMic(onChunk: (pcm: Uint8Array) => void, onLevel: (level: number) => void): Promise<MicCapture> {
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
  const url = URL.createObjectURL(new Blob([WORKLET], { type: 'text/javascript' }))
  try {
    await ctx.audioWorklet.addModule(url)
  } finally {
    URL.revokeObjectURL(url)
  }
  await resumed
  const source = ctx.createMediaStreamSource(stream)
  const tap = new AudioWorkletNode(ctx, 'canopy-listen-tap', { numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1 })
  const silent = ctx.createGain()
  silent.gain.value = 0
  source.connect(tap)
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
  return {
    stop() {
      if (stopped) return
      stopped = true
      tap.port.onmessage = null
      const rest = chunker.flush()
      if (rest) onChunk(rest)
      for (const track of stream.getTracks()) track.stop()
      source.disconnect()
      tap.disconnect()
      void ctx.close()
    },
  }
}
