// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { usePresence } from './usePresence'

class FakeSocket {
  static last: FakeSocket | null = null
  static OPEN = 1
  readyState = 0
  sent: string[] = []
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  constructor(public url: string) {
    FakeSocket.last = this
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
})
