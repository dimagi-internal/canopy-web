// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useReconnectingSocket } from './useReconnectingSocket'

class FakeSocket {
  static all: FakeSocket[] = []
  onopen: (() => void) | null = null
  onmessage: ((m: { data: unknown }) => void) | null = null
  onerror: (() => void) | null = null
  onclose: (() => void) | null = null
  closed = false
  url: string
  constructor(url: string) {
    this.url = url
    FakeSocket.all.push(this)
  }
  close() {
    this.closed = true
  }
  // Test drivers.
  open() { this.onopen?.() }
  send(data: unknown) { this.onmessage?.({ data }) }
  drop() { this.onclose?.() }
}

const last = () => FakeSocket.all[FakeSocket.all.length - 1]

describe('useReconnectingSocket', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    FakeSocket.all = []
    vi.stubGlobal('WebSocket', FakeSocket)
  })
  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('connects, reports open, and hands parsed frames to the latest onFrame', () => {
    const seen: unknown[] = []
    const { result, rerender } = renderHook(({ tag }) => useReconnectingSocket('ws://x/a', (f) => seen.push([tag, f])), {
      initialProps: { tag: 1 },
    })
    expect(FakeSocket.all).toHaveLength(1)
    act(() => last().open())
    expect(result.current.connected).toBe(true)
    rerender({ tag: 2 })
    act(() => last().send('{"a":1}'))
    expect(seen).toEqual([[2, { a: 1 }]])
    expect(FakeSocket.all).toHaveLength(1) // a new callback is not a reconnect
  })

  it('reports a non-JSON frame to onBadFrame and not to onFrame', () => {
    const onFrame = vi.fn()
    const onBadFrame = vi.fn()
    renderHook(() => useReconnectingSocket('ws://x/a', onFrame, { onBadFrame }))
    act(() => last().send('nope'))
    expect(onFrame).not.toHaveBeenCalled()
    expect(onBadFrame).toHaveBeenCalledWith('nope')
  })

  it('reconnects on the 1/2/5/10s ladder and resets it after an open', () => {
    const { result } = renderHook(() => useReconnectingSocket('ws://x/a', () => {}))
    const waits = [1000, 2000, 5000, 10000, 10000]
    for (const w of waits) {
      const before = FakeSocket.all.length
      act(() => last().drop())
      expect(result.current.connected).toBe(false)
      act(() => { vi.advanceTimersByTime(w - 1) })
      expect(FakeSocket.all).toHaveLength(before)
      act(() => { vi.advanceTimersByTime(1) })
      expect(FakeSocket.all).toHaveLength(before + 1)
    }
    act(() => last().open())
    const before = FakeSocket.all.length
    act(() => last().drop())
    act(() => { vi.advanceTimersByTime(1000) })
    expect(FakeSocket.all).toHaveLength(before + 1)
  })

  it('builds the dialled URL per connect (resume cursor)', () => {
    let cursor = 0
    renderHook(() => useReconnectingSocket('ws://x/t/', () => {}, { connectUrl: (u) => `${u}?after=${cursor}` }))
    expect(last().url).toBe('ws://x/t/?after=0')
    cursor = 42
    act(() => last().drop())
    act(() => { vi.advanceTimersByTime(1000) })
    expect(last().url).toBe('ws://x/t/?after=42')
  })

  it('closes on unmount and never reconnects after', () => {
    const { unmount } = renderHook(() => useReconnectingSocket('ws://x/a', () => {}))
    const sock = last()
    unmount()
    expect(sock.closed).toBe(true)
    act(() => sock.drop())
    act(() => { vi.advanceTimersByTime(60_000) })
    expect(FakeSocket.all).toHaveLength(1)
  })

  it('a null url dials nothing; a changed url swaps the socket', () => {
    const { rerender } = renderHook(({ url }) => useReconnectingSocket(url, () => {}), {
      initialProps: { url: null as string | null },
    })
    expect(FakeSocket.all).toHaveLength(0)
    rerender({ url: 'ws://x/1' })
    const first = last()
    rerender({ url: 'ws://x/2' })
    expect(first.closed).toBe(true)
    expect(last().url).toBe('ws://x/2')
  })
})
