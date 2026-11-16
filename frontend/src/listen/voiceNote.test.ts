// A voice note end to end against a fake laptop that holds the real keys: only REST,
// the socket and the microphone are faked.
import { describe, expect, it, vi } from 'vitest'
import type { ListenHello } from '@/api/listen'
import type { ListenDeps } from './controller'
import { AUDIO, controlMessage, newKeyPair, Opener, publicB64, Sealer, sessionKeysLaptop } from './relayCrypto'
import { memoryStore, settle, socketFactory } from './testing'
import { VoiceNote } from './voiceNote'

const SID = '6f1c2a8e-0000-4000-8000-0000000000cc'

async function started() {
  const sockets = socketFactory()
  const hellos: ListenHello[] = []
  const ended: string[] = []
  const mic = { chunk: ((() => undefined) as (pcm: Uint8Array) => void), stopped: false }
  const d: ListenDeps = {
    openSession: vi.fn(async (_runner, kind, hello) => {
      expect(kind).toBe('meeting') // the same session kind: the server can't tell a note from a meeting
      hellos.push(hello)
      return SID
    }),
    endSession: vi.fn(async (id: string) => void ended.push(id)),
    listRunners: vi.fn(async () => []),
    store: memoryStore(),
    socket: sockets.make,
    wsUrl: (path) => `wss://canopy.test/${path}`,
    label: 'iPhone',
    startMic: async (onChunk) => {
      mic.chunk = onChunk
      return { stop: () => void (mic.stopped = true), resume: async () => false }
    },
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
  }
  const laptop = await newKeyPair()
  const pairing = { runner_id: 'runner-1', laptop_pub: await publicB64(laptop.publicKey), laptop_name: 'MBP' }
  const note = new VoiceNote(pairing, d)
  void note.begin()
  await vi.waitFor(() => expect(sockets.sockets).toHaveLength(1))
  const ws = sockets.sockets[0]
  ws.open()
  const hello = hellos[0]
  const keys = await sessionKeysLaptop(laptop.privateKey, hello.phone_eph, hello.phone_key!, SID)
  return { note, ws, mic, ended, fromPhone: new Opener(keys.up, SID), toPhone: new Sealer(keys.down, SID) }
}

describe('VoiceNote', () => {
  it('asks the laptop for a note, sends what was said, and gets the words back', async () => {
    const { note, ws, mic, ended, fromPhone, toPhone } = await started()
    mic.chunk(new Uint8Array([1, 2]))
    ws.binary(await toPhone.control({ type: 'ready', name: 'MBP', nonce: 'n-1', modes: ['meeting', 'note'] }))
    await vi.waitFor(() => expect(ws.sent).toHaveLength(2))
    expect(note.view.status).toBe('recording')
    expect(controlMessage(await fromPhone.open(ws.sent[0]))).toEqual({ type: 'start', mode: 'note', nonce: 'n-1' })
    const held = await fromPhone.open(ws.sent[1])
    expect([held.kind, ...held.body]).toEqual([AUDIO, 1, 2])

    note.stop()
    expect(mic.stopped).toBe(true)
    expect(note.view.status).toBe('transcribing')
    await vi.waitFor(() => expect(ws.sent).toHaveLength(3))
    expect(controlMessage(await fromPhone.open(ws.sent[2]))).toEqual({ type: 'end' })

    ws.binary(await toPhone.control({ type: 'state', state: 'ending', detail: 'transcribing' }))
    ws.binary(await toPhone.control({ type: 'note', text: 'Remind me to call Sam.' }))
    ws.binary(await toPhone.control({ type: 'state', state: 'ended', detail: 'transcribed' }))
    await vi.waitFor(() => expect(note.view.status).toBe('done'))
    expect(note.view.text).toBe('Remind me to call Sam.')
    expect(ended).toEqual([SID])
  })

  it('never asks a laptop that can only do meetings, which would start one', async () => {
    const { note, ws, toPhone } = await started()
    ws.binary(await toPhone.control({ type: 'ready', name: 'MBP', nonce: 'n-1' }))
    await vi.waitFor(() => expect(note.view.status).toBe('failed'))
    expect(note.view.error).toMatch(/Canopy Listen on MBP needs its newest version/)
    expect(ws.sent).toHaveLength(0)
  })

  it('stopped before the laptop answered: reads what was said once it does', async () => {
    const { note, ws, mic, fromPhone, toPhone } = await started()
    mic.chunk(new Uint8Array([7, 7]))
    note.stop()
    expect(note.view.status).toBe('transcribing')
    await settle()
    expect(ws.sent).toHaveLength(0)
    ws.binary(await toPhone.control({ type: 'ready', name: 'MBP', nonce: 'n-2', modes: ['meeting', 'note'] }))
    await vi.waitFor(() => expect(ws.sent).toHaveLength(3))
    const kinds = await Promise.all(ws.sent.map(async (f) => {
      const opened = await fromPhone.open(f)
      return opened.kind === AUDIO ? 'audio' : (controlMessage(opened)?.type as string)
    }))
    expect(kinds).toEqual(['start', 'audio', 'end']) // the end goes after the audio it ends
  })

  it('says so when no words were heard, and cancelling sends a discard', async () => {
    const first = await started()
    first.ws.binary(await first.toPhone.control({ type: 'ready', name: 'MBP', nonce: 'n', modes: ['note'] }))
    await vi.waitFor(() => expect(first.note.view.status).toBe('recording'))
    first.note.stop()
    first.ws.binary(await first.toPhone.control({ type: 'state', state: 'failed', detail: 'no_words' }))
    await vi.waitFor(() => expect(first.note.view.status).toBe('failed'))
    expect(first.note.view.error).toMatch(/No words were heard/)

    const second = await started()
    second.ws.binary(await second.toPhone.control({ type: 'ready', name: 'MBP', nonce: 'n', modes: ['note'] }))
    await vi.waitFor(() => expect(second.note.view.status).toBe('recording'))
    second.note.discard()
    await vi.waitFor(() => expect(second.ws.sent.length).toBeGreaterThanOrEqual(2))
    expect(controlMessage(await second.fromPhone.open(second.ws.sent[0]))?.type).toBe('start')
    expect(controlMessage(await second.fromPhone.open(second.ws.sent.at(-1)!))).toEqual({ type: 'discard' })
    await vi.waitFor(() => expect(second.note.view.status).toBe('discarded'))
  })
})
