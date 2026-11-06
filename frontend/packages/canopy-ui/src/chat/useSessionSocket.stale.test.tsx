// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { STALE_SOCKET_MS, useSessionSocket } from './useSessionSocket'

/**
 * A socket can stay open in name only — a window hidden for a while, a machine
 * that slept — with no close ever firing. The chat then kept its last state
 * ("Thinking…" over a finished reply). Coming back from that, the hook opens a
 * fresh socket, whose snapshot is the server's truth.
 */

const wsUrl = () => 'wss://h/s1'

class FakeSocket {
  static opened: FakeSocket[] = []
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: ((e?: { code?: number }) => void) | null = null
  onerror: (() => void) | null = null
  readyState = 0
  closed = false
  constructor(public url: string) {
    FakeSocket.opened.push(this)
  }
  send() {}
  close() { this.closed = true }
}

let hidden = false
function setHidden(value: boolean) {
  hidden = value
  document.dispatchEvent(new Event('visibilitychange'))
}

beforeEach(() => {
  FakeSocket.opened = []
  hidden = false
  vi.stubGlobal('WebSocket', FakeSocket)
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
  vi.useFakeTimers()
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

function open() {
  const hook = renderHook(() => useSessionSocket({ sessionId: 's1', wsUrl }))
  act(() => { FakeSocket.opened[0].onopen?.() })
  return hook
}

describe('a socket that may have died without closing', () => {
  it('is replaced when the page comes back after being hidden a while', () => {
    open()
    act(() => {
      setHidden(true)
      vi.setSystemTime(Date.now() + STALE_SOCKET_MS + 1000)
      setHidden(false)
    })
    expect(FakeSocket.opened).toHaveLength(2)
    expect(FakeSocket.opened[0].closed).toBe(true)
    // Its late close is ignored: no third socket from the old one's onclose.
    expect(FakeSocket.opened[0].onclose).toBeNull()
  })

  it('is kept across a short glance away', () => {
    open()
    act(() => {
      setHidden(true)
      vi.setSystemTime(Date.now() + 2000)
      setHidden(false)
    })
    expect(FakeSocket.opened).toHaveLength(1)
  })

  it('is replaced when the page’s timers stalled (the machine slept)', () => {
    open()
    act(() => {
      // The heartbeat fires once, then the next one comes a sleep later.
      vi.advanceTimersByTime(20_000)
      vi.setSystemTime(Date.now() + STALE_SOCKET_MS + 1000)
      vi.advanceTimersByTime(20_000)
    })
    expect(FakeSocket.opened).toHaveLength(2)
  })

  it('is replaced when the network comes back', () => {
    open()
    act(() => { window.dispatchEvent(new Event('online')) })
    expect(FakeSocket.opened).toHaveLength(2)
  })
})
