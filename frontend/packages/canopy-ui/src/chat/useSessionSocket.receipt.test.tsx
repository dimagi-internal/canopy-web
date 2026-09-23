// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useSessionSocket } from './useSessionSocket'

/**
 * A send must never vanish. A phone resuming from the background can hold a
 * socket that reads OPEN but delivers nothing, and before this the composer
 * cleared, no line appeared, and the prompt was simply gone (2026-09-23).
 */

class FakeSocket {
  static OPEN = 1
  static last: FakeSocket | null = null
  sent: string[] = []
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  readyState = 1
  constructor(public url: string) {
    FakeSocket.last = this
  }
  send(data: string) {
    this.sent.push(data)
  }
  close() {}
  receive(frame: unknown) {
    this.onmessage?.({ data: JSON.stringify(frame) })
  }
}

const DRAFT = { id: 'd1', slot: 'next', status: 'open', body: '', version: 1, last_editor: 0, last_edit_at: '' }
const SNAPSHOT = {
  event: 'session.state',
  data: { messages: [], active_draft: DRAFT, participants: [], presence_user_ids: [], current_user_id: 1 },
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('WebSocket', FakeSocket)
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

// Stable, as a real caller's is: a new function every render re-keys `connect`
// and opens a fresh socket, which would hide what the first one was sent.
const wsUrl = () => 'wss://h/x'

function user(resendOverHttp?: (text: string, clientId: string) => Promise<unknown>) {
  const hook = renderHook(() =>
    useSessionSocket({ sessionId: 's1', wsUrl, resendOverHttp }),
  )
  act(() => {
    FakeSocket.last!.onopen?.()
    FakeSocket.last!.receive(SNAPSHOT)
  })
  return hook
}

function sentSend() {
  const frame = FakeSocket.last!.sent.map((f) => JSON.parse(f)).find((f) => f.action === 'chat.send')
  return frame?.data as { client_id: string; text: string }
}

describe('a send carries its own text and waits for a receipt', () => {
  it('shows the line at once as pending, and sends the text with a client_id', () => {
    const hook = user()
    act(() => hook.result.current.updateDraft('deploy it'))
    act(() => hook.result.current.sendChat())
    const row = hook.result.current.state.messages.at(-1)!
    expect(row.plaintext).toBe('deploy it')
    expect(row.status).toBe('pending')
    expect(sentSend().text).toBe('deploy it')
    expect(sentSend().client_id).toBeTruthy()
  })

  it('the receipt confirms the SAME row rather than adding a second', () => {
    const hook = user()
    act(() => hook.result.current.updateDraft('deploy it'))
    act(() => hook.result.current.sendChat())
    act(() => FakeSocket.last!.receive({
      event: 'draft.committed',
      data: { draft_id: 'd1', user_message_id: 'transient:abc', client_id: sentSend().client_id },
    }))
    const users = hook.result.current.state.messages.filter((m) => m.role === 'user')
    expect(users).toHaveLength(1)
    expect(users[0].status).toBe('complete')
  })

  it('no receipt: resent over HTTP under the SAME client_id, so it is one turn', async () => {
    const resend = vi.fn().mockResolvedValue({})
    const hook = user(resend)
    act(() => hook.result.current.updateDraft('deploy it'))
    act(() => hook.result.current.sendChat())
    await act(async () => {
      vi.advanceTimersByTime(5_000)
      await Promise.resolve()
    })
    expect(resend).toHaveBeenCalledWith('deploy it', sentSend().client_id)
    expect(hook.result.current.state.messages.at(-1)?.status).toBe('complete')
  })

  it('a socket that closes before the receipt resends at once', async () => {
    const resend = vi.fn().mockResolvedValue({})
    const hook = user(resend)
    act(() => hook.result.current.updateDraft('deploy it'))
    act(() => hook.result.current.sendChat())
    await act(async () => {
      FakeSocket.last!.onclose?.()
      await Promise.resolve()
    })
    expect(resend).toHaveBeenCalledTimes(1)
  })

  it('if the resend fails too, the words stay on screen marked not sent', async () => {
    const hook = user(vi.fn().mockRejectedValue(new Error('offline')))
    act(() => hook.result.current.updateDraft('keep me'))
    act(() => hook.result.current.sendChat())
    await act(async () => {
      vi.advanceTimersByTime(5_000)
      await Promise.resolve()
      await Promise.resolve()
    })
    const row = hook.result.current.state.messages.at(-1)!
    expect(row.plaintext).toBe('keep me')
    expect(row.status).toBe('error')
    expect(row.error_detail).toMatch(/Not sent/)
  })

  it('a reconnect snapshot that does not hold the line yet keeps it on screen', () => {
    const hook = user()
    act(() => hook.result.current.updateDraft('queued behind a running turn'))
    act(() => hook.result.current.sendChat())
    act(() => FakeSocket.last!.receive({
      event: 'draft.committed',
      data: { draft_id: 'd1', user_message_id: 'transient:abc', client_id: sentSend().client_id },
    }))
    // Reconnect: the snapshot has no row for it yet (the agent has not read it).
    act(() => FakeSocket.last!.receive(SNAPSHOT))
    expect(hook.result.current.state.messages.map((m) => m.plaintext)).toEqual([
      'queued behind a running turn',
    ])
    // And once the transcript has it, the snapshot's copy wins: one row.
    act(() => FakeSocket.last!.receive({
      ...SNAPSHOT,
      data: {
        ...SNAPSHOT.data,
        messages: [{
          id: 'm9', turn_index: 90001, role: 'user', content: {}, plaintext: 'queued behind a running turn',
          status: 'complete', error_detail: null, started_at: null, completed_at: null, created_at: '',
        }],
      },
    }))
    expect(hook.result.current.state.messages).toHaveLength(1)
    expect(hook.result.current.state.messages[0].id).toBe('m9')
  })
})
