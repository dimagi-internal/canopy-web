// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useSessionSocket } from './useSessionSocket'

/**
 * An async URL builder: a fresh one-time ticket per connection, so a token
 * never rides the socket URL (URLs land in access logs). A ticket works once,
 * so a reconnect must ask again rather than reuse the first URL.
 */

class FakeSocket {
  static opened: FakeSocket[] = []
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
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

describe('an async socket URL', () => {
  it('opens the socket once the ticket arrives, and asks again on reconnect', async () => {
    let n = 0
    const wsUrl = vi.fn(async () => `wss://h/ws/canopy-sessions/s1/?ticket=t${++n}`)
    renderHook(() => useSessionSocket({ sessionId: 's1', wsUrl }))
    await act(async () => { await Promise.resolve() })
    expect(FakeSocket.opened.map((s) => s.url)).toEqual(['wss://h/ws/canopy-sessions/s1/?ticket=t1'])

    await act(async () => {
      FakeSocket.opened[0].onclose?.()
      await vi.runAllTimersAsync()
    })
    expect(FakeSocket.opened.map((s) => s.url)).toEqual([
      'wss://h/ws/canopy-sessions/s1/?ticket=t1',
      'wss://h/ws/canopy-sessions/s1/?ticket=t2',
    ])
  })

  it('opens nothing for a session it has already left', async () => {
    let release: (url: string) => void = () => {}
    const wsUrl = () => new Promise<string>((resolve) => { release = resolve })
    const { unmount } = renderHook(() => useSessionSocket({ sessionId: 's1', wsUrl }))
    unmount()
    await act(async () => { release('wss://h/late'); await Promise.resolve() })
    expect(FakeSocket.opened).toHaveLength(0)
  })

  it('retries when fetching the ticket fails', async () => {
    let calls = 0
    const wsUrl = async () => {
      calls += 1
      if (calls === 1) throw new Error('network')
      return 'wss://h/ok'
    }
    renderHook(() => useSessionSocket({ sessionId: 's1', wsUrl }))
    await act(async () => { await vi.runAllTimersAsync() })
    expect(FakeSocket.opened.map((s) => s.url)).toEqual(['wss://h/ok'])
  })
})
