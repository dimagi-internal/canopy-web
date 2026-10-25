// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useSessionSocket } from './useSessionSocket'

/**
 * A close canopy MEANS (4003 not yours to open, 4004 no such conversation) is an
 * answer, not a dropped line: the hook stops asking and says so, instead of a
 * chat that reads "Reconnecting…" over an empty transcript forever.
 */

const wsUrl = () => 'wss://h/s1'

class FakeSocket {
  static opened: FakeSocket[] = []
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: ((e?: { code?: number }) => void) | null = null
  onerror: (() => void) | null = null
  readyState = 0
  constructor(public url: string) {
    FakeSocket.opened.push(this)
  }
  send() {}
  close() {}
}

beforeEach(() => {
  FakeSocket.opened = []
  vi.stubGlobal('WebSocket', FakeSocket)
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('a refused connection', () => {
  it.each([4003, 4004])('stops reconnecting on %i and reports it', async (code) => {
    const { result } = renderHook(() => useSessionSocket({ sessionId: 's1', wsUrl }))
    expect(result.current.refused).toBeNull()
    await act(async () => {
      FakeSocket.opened[0].onclose?.({ code })
      await vi.runAllTimersAsync()
    })
    expect(FakeSocket.opened).toHaveLength(1)
    expect(result.current.refused).toEqual({ code, reason: '' })
    expect(result.current.connected).toBe(false)
  })

  it("carries the server's reason when it said one before closing", async () => {
    const { result } = renderHook(() => useSessionSocket({ sessionId: 's1', wsUrl }))
    await act(async () => {
      const ws = FakeSocket.opened[0]
      ws.onopen?.()
      ws.onmessage?.({ data: JSON.stringify({ event: 'session.error', data: { code: 'needs_key', message: 'ask for the key' } }) })
      ws.onclose?.({ code: 4003 })
      await vi.runAllTimersAsync()
    })
    expect(result.current.refused).toEqual({ code: 4003, reason: 'ask for the key' })
  })

  it('still reconnects after an ordinary drop', async () => {
    const { result } = renderHook(() => useSessionSocket({ sessionId: 's1', wsUrl }))
    await act(async () => {
      FakeSocket.opened[0].onclose?.({ code: 1006 })
      await vi.runAllTimersAsync()
    })
    expect(FakeSocket.opened).toHaveLength(2)
    expect(result.current.refused).toBeNull()
  })
})
