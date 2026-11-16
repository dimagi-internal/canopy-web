// The phone's socket: no audio before `ready`, one ordered queue sealed as it leaves,
// audio held through a gap (bounded) and sent paced, reconnects for minutes, and the
// server's close codes in words.
import { describe, expect, it, vi } from 'vitest'
import { DRAIN_PER_SECOND, MAX_BACKLOG_CHUNKS, PhoneRelay, RECONNECT_FOR_MS, type RelayEvents } from './relay'
import { AUDIO, CONTROL, Opener, Sealer, controlMessage } from './relayCrypto'
import { settle, socketFactory } from './testing'

const SID = 'sid-relay-test'
const UP = new Uint8Array(32).fill(1) as Uint8Array<ArrayBuffer>
const DOWN = new Uint8Array(32).fill(2) as Uint8Array<ArrayBuffer>

function events(): RelayEvents & { calls: Record<string, unknown[]> } {
  const calls: Record<string, unknown[]> = { control: [], peer: [], ended: [], lost: [], reconnecting: [], resync: [], waits: [] }
  return {
    calls,
    onControl: (m) => calls.control.push(m),
    onPeer: (c) => calls.peer.push(c),
    onEnded: (r) => calls.ended.push(r),
    onLost: (w) => calls.lost.push(w),
    onReconnecting: () => calls.reconnecting.push(true),
    onResync: () => calls.resync.push(true),
  }
}

async function setup(now: () => number = Date.now) {
  const sockets = socketFactory()
  const ev = events()
  const wait = async (ms: number) => void ev.calls.waits.push(ms)
  const relay = new PhoneRelay('wss://x/ws/listen/s/?role=phone', new Sealer(UP, SID), new Opener(DOWN, SID), ev, sockets.make, now, 5, wait)
  const connected = relay.connect()
  sockets.sockets[0].open()
  await connected
  const laptopSends = new Sealer(DOWN, SID)
  const laptopOpens = new Opener(UP, SID)
  return { relay, ev, socket: () => sockets.sockets.at(-1)!, sockets, laptopSends, laptopOpens }
}

describe('PhoneRelay', () => {
  it('holds audio until the laptop is ready, keeps the newest 3 minutes, and sends start first', async () => {
    const { relay, ev, socket, laptopSends, laptopOpens } = await setup()
    for (let i = 0; i < MAX_BACKLOG_CHUNKS + 5; i++) relay.sendAudio(new Uint8Array([i % 256, i >> 8]))
    await settle()
    expect(socket().sent).toHaveLength(0)
    expect(relay.backlogSeconds).toBe(MAX_BACKLOG_CHUNKS / 2)
    // As the meeting does: `start` is queued from inside the `ready` callback.
    const onControl = ev.onControl
    ev.onControl = (m) => {
      onControl(m)
      if (m.type === 'ready') void relay.sendControl({ type: 'start', notice_accepted: true })
    }
    socket().binary(await laptopSends.control({ type: 'ready', name: 'MBP' }))
    await vi.waitFor(() => expect(socket().sent).toHaveLength(MAX_BACKLOG_CHUNKS + 1))
    expect(ev.calls.control).toEqual([{ type: 'ready', name: 'MBP' }])
    const opened = []
    for (const f of socket().sent) opened.push(await laptopOpens.open(f)) // counters in order, or this throws
    expect(controlMessage(opened[0])).toEqual({ type: 'start', notice_accepted: true })
    const first = opened[1].body
    expect(first[0] + (first[1] << 8)).toBe(5) // the oldest 5 chunks were let go
    expect(opened.slice(1).every((o) => o.kind === AUDIO)).toBe(true)
    // Caught up paced: a wait each time a second's worth went out.
    expect(ev.calls.waits.length).toBeGreaterThanOrEqual(Math.floor(MAX_BACKLOG_CHUNKS / DRAIN_PER_SECOND) - 1)
    expect(relay.backlogSeconds).toBe(0)
  })

  it('holds audio while the laptop is away, then catches up and asks to resync', async () => {
    const { relay, ev, socket, laptopSends, laptopOpens } = await setup()
    socket().binary(await laptopSends.control({ type: 'ready', name: 'MBP' }))
    await vi.waitFor(() => expect(relay.ready).toBe(true))
    socket().text({ type: 'peer', role: 'laptop', connected: false })
    relay.sendAudio(new Uint8Array([1]))
    relay.sendAudio(new Uint8Array([2]))
    void relay.sendControl({ type: 'end' })
    await settle()
    expect(socket().sent).toHaveLength(0)
    socket().text({ type: 'peer', role: 'laptop', connected: true })
    await vi.waitFor(() => expect(socket().sent).toHaveLength(3))
    expect(ev.calls.resync).toHaveLength(1)
    const opened = []
    for (const f of socket().sent) opened.push(await laptopOpens.open(f))
    expect(opened.map((o) => o.kind === AUDIO ? o.body[0] : controlMessage(o)!.type)).toEqual([1, 2, 'end']) // end after the audio
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

  it('keeps reopening a dropped socket, sends what was held, asks to resync, and gives up only after minutes', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout'] })
    let clock = 1_000_000
    try {
      const { relay, ev, sockets, socket, laptopSends, laptopOpens } = await setup(() => clock)
      socket().binary(await laptopSends.control({ type: 'ready', name: 'MBP' }))
      await vi.waitFor(() => expect(relay.ready).toBe(true))
      socket().drop(1006)
      expect(ev.calls.reconnecting).toHaveLength(1)
      relay.sendAudio(new Uint8Array([7]))
      await vi.advanceTimersByTimeAsync(10)
      expect(sockets.sockets).toHaveLength(2)
      socket().drop(1006) // the first try fails: still offline
      clock += 20
      await vi.advanceTimersByTimeAsync(100)
      expect(sockets.sockets).toHaveLength(3)
      socket().open()
      await vi.waitFor(() => expect(socket().sent).toHaveLength(1))
      expect((await laptopOpens.open(socket().sent[0])).body[0]).toBe(7)
      expect(ev.calls.resync).toHaveLength(1)
      expect(ev.calls.lost).toEqual([])

      socket().drop(1006) // and again, much later: still not given up
      for (let i = 0; i < 40 && ev.calls.lost.length === 0; i++) {
        clock += 10_000
        await vi.advanceTimersByTimeAsync(100)
        socket().drop(1006)
      }
      expect(ev.calls.lost).toEqual(['dropped'])
      expect(clock - 1_000_000).toBeGreaterThan(RECONNECT_FOR_MS)
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
