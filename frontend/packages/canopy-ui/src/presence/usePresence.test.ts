// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { usePresence } from './usePresence'

class FakeSocket {
  static last: FakeSocket | null = null
  static created = 0
  static OPEN = 1
  readyState = 0
  sent: string[] = []
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  constructor(public url: string) {
    FakeSocket.last = this
    FakeSocket.created += 1
  }
  send(frame: string) {
    this.sent.push(frame)
  }
  close() {
    this.readyState = 3
  }
  open() {
    this.readyState = FakeSocket.OPEN
    this.onopen?.()
  }
  deliver(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) })
  }
}

beforeEach(() => {
  vi.stubGlobal('WebSocket', FakeSocket as unknown as typeof WebSocket)
  vi.useFakeTimers()
  FakeSocket.created = 0
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  FakeSocket.last = null
})

const LOC = { pageKey: 'ace:ws:opp:a/run-001', subLocation: 'run overview' }

describe('usePresence', () => {
  it('sends presence.enter once the socket opens', () => {
    renderHook(() => usePresence({ url: 'ws://x/ws/presence/', location: LOC }))
    act(() => FakeSocket.last!.open())
    expect(JSON.parse(FakeSocket.last!.sent[0])).toEqual({
      type: 'presence.enter',
      page_key: LOC.pageKey,
      sub_location: LOC.subLocation,
    })
  })

  it('exposes the roster the server broadcasts', () => {
    const { result } = renderHook(() => usePresence({ url: 'ws://x/ws/presence/', location: LOC }))
    act(() => FakeSocket.last!.open())
    act(() =>
      FakeSocket.last!.deliver({
        event: 'presence.roster',
        data: {
          page_key: LOC.pageKey,
          viewers: [{ email: 'a@x.com', name: 'A', sub_location: 'idea-to-pdd', idle: false, self: true }],
        },
      }),
    )
    expect(result.current.viewers).toEqual([
      { email: 'a@x.com', name: 'A', subLocation: 'idea-to-pdd', idle: false, self: true },
    ])
  })

  it('ignores a roster for a page key it is no longer on', () => {
    const { result } = renderHook(() => usePresence({ url: 'ws://x/ws/presence/', location: LOC }))
    act(() => FakeSocket.last!.open())
    act(() =>
      FakeSocket.last!.deliver({
        event: 'presence.roster',
        data: { page_key: 'ace:ws:opp:STALE/run-999', viewers: [
          { email: 'z@x.com', name: 'Z', sub_location: '', idle: false, self: false }] },
      }),
    )
    expect(result.current.viewers).toEqual([])
  })

  it('re-enters without reconnecting when the location changes', () => {
    const { rerender } = renderHook(
      ({ location }) => usePresence({ url: 'ws://x/ws/presence/', location }),
      { initialProps: { location: LOC } },
    )
    act(() => FakeSocket.last!.open())
    const socket = FakeSocket.last!
    rerender({ location: { pageKey: 'ace:ws:activity', subLocation: 'Activity' } })
    expect(FakeSocket.last).toBe(socket) // same socket, no reconnect
    expect(JSON.parse(socket.sent[socket.sent.length - 1])).toEqual({
      type: 'presence.enter',
      page_key: 'ace:ws:activity',
      sub_location: 'Activity',
    })
  })

  it('heartbeats every 20 seconds', () => {
    renderHook(() => usePresence({ url: 'ws://x/ws/presence/', location: LOC }))
    act(() => FakeSocket.last!.open())
    act(() => void vi.advanceTimersByTime(20_000))
    expect(JSON.parse(FakeSocket.last!.sent[1])).toEqual({ type: 'presence.heartbeat', idle: false })
  })

  it('clears the roster and sends nothing when there is no location', () => {
    const { result } = renderHook(() =>
      usePresence({ url: 'ws://x/ws/presence/', location: null }),
    )
    expect(result.current.viewers).toEqual([])
    expect(FakeSocket.last).toBeNull()
  })

  it('backs off exponentially while reconnects keep failing, up to a ceiling', () => {
    // A tab left open past session expiry is closed with 4001 on every
    // attempt. A flat retry hammers a handshake every 2s forever; the delay
    // must grow and then cap.
    renderHook(() => usePresence({ url: 'ws://x/ws/presence/', location: LOC }))
    expect(FakeSocket.created).toBe(1)

    const failOnce = (expectedDelay: number) => {
      const before = FakeSocket.created
      act(() => FakeSocket.last!.onclose!())
      // Nothing reconnects a millisecond early...
      act(() => void vi.advanceTimersByTime(expectedDelay - 1))
      expect(FakeSocket.created).toBe(before)
      // ...and exactly one reconnect lands on the deadline.
      act(() => void vi.advanceTimersByTime(1))
      expect(FakeSocket.created).toBe(before + 1)
    }

    failOnce(2_000)
    failOnce(4_000)
    failOnce(8_000)
    failOnce(16_000)
    failOnce(32_000)
    failOnce(60_000) // ceiling, not 64_000
    failOnce(60_000) // and it stays there
  })

  it('resets the backoff once a socket actually opens', () => {
    renderHook(() => usePresence({ url: 'ws://x/ws/presence/', location: LOC }))
    act(() => FakeSocket.last!.onclose!())
    act(() => void vi.advanceTimersByTime(2_000))
    act(() => FakeSocket.last!.onclose!())
    act(() => void vi.advanceTimersByTime(4_000)) // now at a 4s delay

    act(() => FakeSocket.last!.open()) // a good connection clears the streak

    const before = FakeSocket.created
    act(() => FakeSocket.last!.onclose!())
    act(() => void vi.advanceTimersByTime(2_000))
    expect(FakeSocket.created).toBe(before + 1)
  })

  it('clears the previous page\'s viewers on navigation even when the socket is not open', () => {
    // Otherwise the old page's avatars linger on the new page until a roster
    // for it arrives — which, on a dead socket, may be never.
    const { result, rerender } = renderHook(
      ({ location }) => usePresence({ url: 'ws://x/ws/presence/', location }),
      { initialProps: { location: LOC } },
    )
    act(() => FakeSocket.last!.open())
    act(() =>
      FakeSocket.last!.deliver({
        event: 'presence.roster',
        data: {
          page_key: LOC.pageKey,
          viewers: [{ email: 'a@x.com', name: 'A', sub_location: '', idle: false, self: false }],
        },
      }),
    )
    expect(result.current.viewers).toHaveLength(1)

    FakeSocket.last!.readyState = 3 // socket dropped, reconnect pending
    rerender({ location: { pageKey: 'ace:ws:activity', subLocation: 'Activity' } })
    expect(result.current.viewers).toEqual([])
  })

  it('swallows a synchronous WebSocket constructor throw and stays on an empty roster', () => {
    class ThrowingSocket {
      constructor() {
        throw new Error('mixed content: refused to connect to insecure WebSocket')
      }
    }
    vi.stubGlobal('WebSocket', ThrowingSocket as unknown as typeof WebSocket)

    let hook!: ReturnType<typeof renderHook<{ viewers: unknown[] }, void>>
    expect(() => {
      hook = renderHook(() => usePresence({ url: 'ws://x/ws/presence/', location: LOC }))
    }).not.toThrow()
    expect(hook.result.current.viewers).toEqual([])
  })
})
