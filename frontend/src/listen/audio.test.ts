import { describe, expect, it } from 'vitest'
import { CHUNK_SAMPLES, Downsampler, PcmChunker, rms, toInt16LE } from './audio'

describe('Downsampler', () => {
  it('turns 48 kHz into 16 kHz by averaging each group of three', () => {
    const out = new Downsampler(48000).push(Float32Array.from([0.3, 0.6, 0.9, -0.3, -0.3, -0.3]))
    expect(Array.from(out).map((v) => +v.toFixed(5))).toEqual([0.6, -0.3])
  })

  it('handles a rate that does not divide evenly, across blocks, without drifting', () => {
    const down = new Downsampler(44100)
    let produced = 0
    for (let i = 0; i < 100; i++) produced += down.push(new Float32Array(441).fill(0.5)).length
    expect(produced).toBe(16000) // 44 100 in, exactly a second out
    const dc = new Downsampler(44100).push(new Float32Array(4410).fill(0.25))
    expect(dc.every((v) => Math.abs(v - 0.25) < 1e-6)).toBe(true)
  })

  it('refuses a microphone slower than 16 kHz', () => {
    expect(() => new Downsampler(8000)).toThrow()
  })
})

describe('PCM', () => {
  it('is int16 little-endian, clamped', () => {
    const bytes = toInt16LE([0, 1, -1, 2, 0.5])
    const view = new DataView(bytes.buffer)
    expect([0, 1, 2, 3, 4].map((i) => view.getInt16(i * 2, true))).toEqual([0, 32767, -32768, 32767, 16384])
    expect(bytes[2]).toBe(0xff) // low byte first
    expect(bytes[3]).toBe(0x7f)
  })

  it('comes in half-second chunks, the remainder on flush', () => {
    const chunker = new PcmChunker()
    const chunks = chunker.push(new Float32Array(CHUNK_SAMPLES * 2 + 100))
    expect(chunks.map((c) => c.length)).toEqual([CHUNK_SAMPLES * 2, CHUNK_SAMPLES * 2])
    expect(chunker.flush()?.length).toBe(200)
    expect(chunker.flush()).toBeNull()
  })

  it('measures a level', () => {
    expect(rms(new Float32Array([0.5, -0.5]))).toBeCloseTo(0.5)
    expect(rms(new Float32Array())).toBe(0)
  })
})
