// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { RECONNECT_DELAYS_MS, useSessionSocket } from './useSessionSocket'

/**
 * A socket that keeps failing before it opens (the server answers the handshake
 * with a 500, which the browser reports as 1006) backs off to a minute, so one
 * broken conversation in an open tab cannot hammer the server on its own. A
 * socket that opened and then dropped starts the climb again from the bottom.
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

/** Fail the newest socket before it opens, and report how long the hook waited. */
async function failAndMeasure(): Promise<number> {
  const before = FakeSocket.opened.length
  await act(async () => {
    FakeSocket.opened[before - 1].onclose?.({ code: 1006 })
  })
  let waited = 0
  while (FakeSocket.opened.length === before) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000)
    })
    waited += 1_000
    if (waited > 120_000) throw new Error('never reconnected')
  }
  return waited
}

describe('reconnect backoff', () => {
  it('climbs to a minute while the handshake keeps failing, and stays there', async () => {
    renderHook(() => useSessionSocket({ sessionId: 's1', wsUrl }))
    const waits: number[] = []
    for (let i = 0; i < RECONNECT_DELAYS_MS.length + 2; i++) waits.push(await failAndMeasure())
    expect(waits).toEqual([...RECONNECT_DELAYS_MS, 60_000, 60_000])
    // Five minutes of a broken conversation is a handful of attempts, not dozens.
    expect(Math.max(...waits)).toBe(60_000)
  })

  it('starts again from a second once a socket has opened', async () => {
    renderHook(() => useSessionSocket({ sessionId: 's1', wsUrl }))
    for (let i = 0; i < 4; i++) await failAndMeasure()
    await act(async () => {
      FakeSocket.opened[FakeSocket.opened.length - 1].onopen?.()
    })
    expect(await failAndMeasure()).toBe(1_000)
  })
})
