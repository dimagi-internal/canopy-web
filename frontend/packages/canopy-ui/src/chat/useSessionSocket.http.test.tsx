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
    expect(sendOverHttp).toHaveBeenCalledWith('hello ace', expect.any(String))
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

  it('stamps the SAME client_id on the optimistic row and the HTTP body', async () => {
    // The server's queued-turn projection carries back whatever client_id the
    // POST sent (apps/canopy_sessions/services.py::queued_messages parses it
    // off the turn's idempotency_key). Without a matching id on this row's
    // `content`, `QueuedRows.hideClientIds` cannot recognise the two as one
    // send, and a contact's (or the widget's) own send rendered twice — once
    // as their bubble, once as an unowned "queued" placeholder, since neither
    // path gives a contact an `author.user_id` to compare against.
    const { hook, sendOverHttp } = contact()
    act(() => hook.result.current.updateDraft('please help'))
    await act(async () => hook.result.current.sendChat())
    const sentClientId = sendOverHttp.mock.calls[0][1] as string
    expect(typeof sentClientId).toBe('string')
    expect(sentClientId.length).toBeGreaterThan(0)
    expect(hook.result.current.state.messages.at(-1)?.content?.client_id).toBe(sentClientId)
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

  it('does not clobber newer typing with a stale failed send', async () => {
    // 2026-09-26 review: contact sends "hello" (draft clears to null), starts
    // typing "world" while the POST is still in flight, "hello"'s request
    // fails — the restore must not overwrite "world" with "hello". The old
    // `updateLocalDraft(body)` closed over "hello" at send time and always
    // won; the fix (`restoreFailedLocalDraft`) only restores when nothing
    // newer has been typed since (the draft is still null/empty).
    let rejectSend!: (err: Error) => void
    const pending: Promise<unknown> = new Promise((_resolve, reject) => {
      rejectSend = reject
    })
    const sendOverHttp = vi.fn().mockReturnValue(pending)
    const { hook } = contact(sendOverHttp)

    act(() => hook.result.current.updateDraft('hello'))
    act(() => {
      hook.result.current.sendChat()
    })
    expect(hook.result.current.state.active_draft).toBeNull()

    // Typed BEFORE the request resolves — this must win.
    act(() => hook.result.current.updateDraft('world'))
    expect(hook.result.current.state.active_draft?.body).toBe('world')

    await act(async () => {
      rejectSend(new Error('network blip'))
      await pending.catch(() => undefined)
    })

    expect(hook.result.current.state.active_draft?.body).toBe('world')
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
