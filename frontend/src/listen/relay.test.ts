// The phone's socket: no audio before `ready` (at most 10 s held), counters in order,
// one reconnect, and the server's close codes in words.
import { describe, expect, it, vi } from 'vitest'
import { MAX_BUFFERED_AUDIO, PhoneRelay, type RelayEvents } from './relay'
import { AUDIO, CONTROL, Opener, Sealer, controlMessage } from './relayCrypto'
import { settle, socketFactory } from './testing'

const SID = 'sid-relay-test'
const UP = new Uint8Array(32).fill(1) as Uint8Array<ArrayBuffer>
const DOWN = new Uint8Array(32).fill(2) as Uint8Array<ArrayBuffer>

function events(): RelayEvents & { calls: Record<string, unknown[]> } {
  const calls: Record<string, unknown[]> = { control: [], peer: [], ended: [], lost: [], reconnecting: [] }
  return {
    calls,
    onControl: (m) => calls.control.push(m),
    onPeer: (c) => calls.peer.push(c),
    onEnded: (r) => calls.ended.push(r),
    onLost: (w) => calls.lost.push(w),
    onReconnecting: () => calls.reconnecting.push(true),
  }
}

async function setup() {
  const sockets = socketFactory()
  const ev = events()
  const relay = new PhoneRelay('wss://x/ws/listen/s/?role=phone', new Sealer(UP, SID), new Opener(DOWN, SID), ev, sockets.make, Date.now, 5)
  const connected = relay.connect()
  sockets.sockets[0].open()
  await connected
  const laptopSends = new Sealer(DOWN, SID)
  const laptopOpens = new Opener(UP, SID)
  return { relay, ev, socket: () => sockets.sockets.at(-1)!, sockets, laptopSends, laptopOpens }
}

describe('PhoneRelay', () => {
  it('holds audio until the laptop is ready, keeping only the last 10 s', async () => {
    const { relay, ev, socket, laptopSends, laptopOpens } = await setup()
    for (let i = 0; i < MAX_BUFFERED_AUDIO + 5; i++) relay.sendAudio(new Uint8Array([i]))
    await settle()
    expect(socket().sent).toHaveLength(0)
    socket().binary(await laptopSends.control({ type: 'ready', name: 'MBP' }))
    await settle()
    expect(ev.calls.control).toEqual([{ type: 'ready', name: 'MBP' }])
    expect(relay.ready).toBe(true)
    const opened = []
    for (const f of socket().sent) opened.push(await laptopOpens.open(f))
    expect(opened.map((o) => o.kind)).toEqual(Array(MAX_BUFFERED_AUDIO).fill(AUDIO))
    expect(opened.map((o) => o.body[0])).toEqual(Array.from({ length: MAX_BUFFERED_AUDIO }, (_, i) => i + 5))
  })

  it('sends control messages before ready, and passes on server notices', async () => {
    const { relay, ev, socket, laptopOpens } = await setup()
    await relay.sendControl({ type: 'pair', phone_key: 'k', label: 'iPhone' })
    const opened = await laptopOpens.open(socket().sent[0])
    expect(opened.kind).toBe(CONTROL)
    expect(controlMessage(opened)).toEqual({ type: 'pair', phone_key: 'k', label: 'iPhone' })
    socket().text({ type: 'peer', role: 'laptop', connected: true })
    socket().text({ type: 'ended', reason: 'ended_by_owner' })
    expect(ev.calls.peer).toEqual([true])
    expect(ev.calls.ended).toEqual(['ended_by_owner'])
  })

  it('drops a forged or replayed frame without a word', async () => {
    const { ev, socket, laptopSends } = await setup()
    const frame = await laptopSends.control({ type: 'state', state: 'recording' })
    socket().binary(frame)
    socket().binary(frame)
    const forged = (await laptopSends.control({ type: 'chat', url: 'https://x' })).slice()
    forged[forged.length - 1] ^= 1
    socket().binary(forged)
    await settle()
    expect(ev.calls.control).toEqual([{ type: 'state', state: 'recording' }])
  })

  it('reopens a dropped socket once, sending what was queued meanwhile, then gives up', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout'] })
    try {
      const { relay, ev, sockets, socket, laptopSends, laptopOpens } = await setup()
      socket().binary(await laptopSends.control({ type: 'ready', name: 'MBP' }))
      await vi.waitFor(() => expect(relay.ready).toBe(true))
      socket().drop(1006)
      expect(ev.calls.reconnecting).toHaveLength(1)
      relay.sendAudio(new Uint8Array([7]))
      await vi.advanceTimersByTimeAsync(10)
      expect(sockets.sockets).toHaveLength(2)
      socket().open()
      await vi.waitFor(() => expect(socket().sent).toHaveLength(1))
      expect((await laptopOpens.open(socket().sent[0])).body[0]).toBe(7)
      socket().drop(1006)
      expect(ev.calls.lost).toEqual(['dropped'])
    } finally {
      vi.useRealTimers()
    }
  })

  it('treats the server refusing this socket as final', async () => {
    const { ev, socket } = await setup()
    socket().drop(4009)
    expect(ev.calls.lost).toEqual(['taken'])
    expect(ev.calls.reconnecting).toHaveLength(0)
  })

  it('closing on purpose is silent', async () => {
    const { relay, ev, socket } = await setup()
    relay.close()
    socket().drop(1000)
    expect(ev.calls.lost).toEqual([])
  })
})
