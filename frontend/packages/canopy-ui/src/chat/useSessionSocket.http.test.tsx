// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useSessionSocket } from './useSessionSocket'

/**
 * A read-only principal — a canopy CONTACT — chats through the same hook: the
 * message goes out over HTTP, the draft stays local, and nothing is written to
 * the socket (which would refuse it).
 */

class FakeSocket {
  static OPEN = 1
  static sent: string[] = []
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  readyState = 1
  constructor(public url: string) {}
  send(data: string) {
    FakeSocket.sent.push(data)
  }
  close() {}
}

beforeEach(() => {
  FakeSocket.sent = []
  vi.stubGlobal('WebSocket', FakeSocket)
})
afterEach(() => vi.unstubAllGlobals())

function contact(sendOverHttp = vi.fn().mockResolvedValue({})) {
  const hook = renderHook(() =>
    useSessionSocket({ sessionId: 's1', wsUrl: () => 'wss://h/x', sendOverHttp }),
  )
  return { hook, sendOverHttp }
}

describe('sending over HTTP', () => {
  it('types into a local draft and sends it over HTTP, never the socket', async () => {
    const { hook, sendOverHttp } = contact()
    act(() => hook.result.current.updateDraft('hello ace'))
    expect(hook.result.current.state.active_draft?.body).toBe('hello ace')
    await act(async () => hook.result.current.sendChat())
    expect(sendOverHttp).toHaveBeenCalledWith('hello ace')
    expect(FakeSocket.sent.filter((f) => f.includes('chat.send') || f.includes('draft.update'))).toEqual([])
  })

  it('shows the line and waits for the reply, as a user send does', async () => {
    const { hook } = contact()
    act(() => hook.result.current.updateDraft('are we on track?'))
    await act(async () => hook.result.current.sendChat())
    expect(hook.result.current.awaitingReply).toBe(true)
    expect(hook.result.current.state.messages.at(-1)?.plaintext).toBe('are we on track?')
    expect(hook.result.current.state.active_draft).toBeNull()
  })

  it('a failed send puts the words back and says why', async () => {
    const { hook } = contact(vi.fn().mockRejectedValue(new Error('canopy said 429')))
    act(() => hook.result.current.updateDraft('keep me'))
    await act(async () => {
      hook.result.current.sendChat()
      await Promise.resolve()
    })
    expect(hook.result.current.state.active_draft?.body).toBe('keep me')
    expect(hook.result.current.lastError).toBe('canopy said 429')
    expect(hook.result.current.awaitingReply).toBe(false)
  })

  it('an empty draft sends nothing', async () => {
    const { hook, sendOverHttp } = contact()
    act(() => hook.result.current.updateDraft('   '))
    await act(async () => hook.result.current.sendChat())
    expect(sendOverHttp).not.toHaveBeenCalled()
  })

  it('without it, a user sends over the socket exactly as before', async () => {
    const hook = renderHook(() => useSessionSocket({ sessionId: 's1', wsUrl: () => 'wss://h/x' }))
    act(() => hook.result.current.updateDraft('hello'))
    await act(async () => hook.result.current.sendChat())
    expect(FakeSocket.sent.some((f) => f.includes('chat.send'))).toBe(true)
  })
})
