// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Message } from './protocol'
import { useSessionSocket } from './useSessionSocket'

class FakeSocket {
  static OPEN = 1
  static last: FakeSocket | null = null
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: ((e?: { code?: number }) => void) | null = null
  onerror: (() => void) | null = null
  readyState = 1
  constructor(public url: string) {
    FakeSocket.last = this
  }
  send() {}
  close() {}
}

beforeEach(() => {
  FakeSocket.last = null
  vi.stubGlobal('WebSocket', FakeSocket)
})
afterEach(() => vi.unstubAllGlobals())

const snapshot = (messages: unknown[] = []) => ({
  data: JSON.stringify({
    event: 'session.state',
    data: { messages, active_draft: null, participants: [], presence_user_ids: [], current_user_id: 1 },
  }),
})

describe('synced', () => {
  it('is false from open until the snapshot lands, then true', () => {
    const hook = renderHook(() => useSessionSocket({ sessionId: 's1', wsUrl: () => 'wss://h/x' }))
    const ws = FakeSocket.last!
    act(() => ws.onopen?.())
    expect(hook.result.current.connected).toBe(true)
    expect(hook.result.current.synced).toBe(false)
    act(() => ws.onmessage?.(snapshot()))
    expect(hook.result.current.synced).toBe(true)
  })
})

describe('onTitleUpdated', () => {
  it('is handed the new title', () => {
    const onTitleUpdated = vi.fn()
    renderHook(() => useSessionSocket({ sessionId: 's1', wsUrl: () => 'wss://h/x', onTitleUpdated }))
    act(() => FakeSocket.last!.onmessage?.({ data: JSON.stringify({ event: 'session.title_updated', data: { title: 'Fresh name' } }) }))
    expect(onTitleUpdated).toHaveBeenCalledWith('Fresh name')
  })
})

describe('prependMessages', () => {
  it('records that nothing older remains, and a reconnect keeps both rows and answer', () => {
    const hook = renderHook(() => useSessionSocket({ sessionId: 's1', wsUrl: () => 'wss://h/x' }))
    const row = (i: number): Message => ({
      id: `r${i}`, turn_index: i, role: 'assistant', content: {}, plaintext: `${i}`, status: 'complete',
      error_detail: null, started_at: null, completed_at: null, created_at: '2026-10-31T10:00:00Z',
    })
    act(() => FakeSocket.last!.onmessage?.(snapshot([row(5), row(6)])))
    act(() => hook.result.current.prependMessages([row(1), row(2), row(3), row(4)], { hasMoreBefore: false }))
    expect(hook.result.current.state.has_more_before).toBe(false)
    act(() => FakeSocket.last!.onmessage?.(snapshot([row(5), row(6), row(7)])))
    expect(hook.result.current.state.messages.map((m) => m.turn_index)).toEqual([1, 2, 3, 4, 5, 6, 7])
    expect(hook.result.current.state.has_more_before).toBe(false)
  })
})

describe('every send goes through one pending → delivered / failed path', () => {
  function contact(sendOverHttp: (t: string, c: string) => Promise<unknown>) {
    return renderHook(() => useSessionSocket({ sessionId: 's1', wsUrl: () => 'wss://h/x', sendOverHttp }))
  }

  it("a contact's send is pending until HTTP answers, then delivered", async () => {
    let resolve!: () => void
    const hook = contact(() => new Promise<void>((r) => { resolve = r }))
    act(() => hook.result.current.sendChat('hello'))
    expect(hook.result.current.state.messages[0].status).toBe('pending')
    await act(async () => resolve())
    expect(hook.result.current.state.messages[0].status).toBe('complete')
  })

  it("a contact's failed send is marked not sent, never left as if sent", async () => {
    const hook = contact(() => Promise.reject(new Error('canopy said 429')))
    await act(async () => {
      hook.result.current.sendChat('hello')
      await Promise.resolve()
    })
    expect(hook.result.current.state.messages[0]).toMatchObject({ status: 'error', plaintext: 'hello' })
    expect(hook.result.current.state.messages[0].error_detail).toContain('canopy said 429')
  })

  it("noteLocalSend shows a host's HTTP send as delivered, once", () => {
    const hook = renderHook(() => useSessionSocket({ sessionId: 's1', wsUrl: () => 'wss://h/x' }))
    act(() => hook.result.current.noteLocalSend('first message', 'c1'))
    act(() => hook.result.current.noteLocalSend('first message', 'c1'))
    expect(hook.result.current.state.messages).toHaveLength(1)
    expect(hook.result.current.state.messages[0]).toMatchObject({ status: 'complete', content: { client_id: 'c1' } })
    expect(hook.result.current.awaitingReply).toBe(true)
  })
})
