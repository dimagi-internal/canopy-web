// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Message, SessionState } from 'canopy-ui/chat'

const getSession = vi.fn()
const recordEvents = vi.fn(() => Promise.resolve())
vi.mock('@/api/chat', () => ({ getSession: (...a: unknown[]) => getSession(...a) }))
vi.mock('@/api/logs', () => ({ recordEvents: (...a: unknown[]) => recordEvents(...(a as [])) }))

import { RECHECK_MS, SETTLE_DELAY_MS, useTailCheck } from './useTailCheck'

const msg = (id: string, turn: number, role = 'assistant'): Message => ({
  id, turn_index: turn, role, content: {}, plaintext: id, status: 'complete',
  error_detail: null, started_at: null, completed_at: null, created_at: '',
} as Message)

const rest = (id: string, turn: number, role = 'assistant') =>
  ({ id, turn_index: turn, role, content: {}, plaintext: id, author: null, created_at: '' })

function state(messages: Message[], finished: string | null): SessionState {
  return {
    messages, active_draft: null, participants: [], presence_user_ids: [], current_user_id: 7,
    turn_status: finished ? ({ state: 'done', settled: true, finished_at: finished } as SessionState['turn_status']) : null,
  }
}

describe('useTailCheck', () => {
  beforeEach(() => { vi.useFakeTimers(); getSession.mockReset(); recordEvents.mockClear() })
  afterEach(() => vi.useRealTimers())

  it('after a turn ends, puts back a line the page lost and records that it had to', async () => {
    getSession.mockResolvedValue({
      messages: [rest('u1', 64, 'user'), rest('a1', 128)], has_more_before: false, oldest_loaded_turn_index: 64,
    })
    const resync = vi.fn()
    renderHook(() => useTailCheck('s1', state([msg('a1', 128)], '2026-11-02T15:30:08Z'), resync))
    await act(async () => { await vi.advanceTimersByTimeAsync(SETTLE_DELAY_MS + RECHECK_MS + 10) })
    expect(resync).toHaveBeenCalledTimes(1)
    expect(resync.mock.calls[0][0].map((m: Message) => m.id)).toEqual(['u1', 'a1'])
    expect(recordEvents).toHaveBeenCalledWith([expect.objectContaining({ kind: 'chat.diverged', key: 's1' })])
  })

  it('changes nothing when the page already matches', async () => {
    getSession.mockResolvedValue({ messages: [rest('a1', 128)], has_more_before: false })
    const resync = vi.fn()
    renderHook(() => useTailCheck('s1', state([msg('a1', 128)], '2026-11-02T15:30:08Z'), resync))
    await act(async () => { await vi.advanceTimersByTimeAsync(SETTLE_DELAY_MS + RECHECK_MS + 10) })
    expect(resync).not.toHaveBeenCalled()
    expect(recordEvents).not.toHaveBeenCalled()
  })

  it('does not look while a turn is running', async () => {
    const resync = vi.fn()
    renderHook(() => useTailCheck('s1', state([msg('a1', 128)], null), resync))
    await act(async () => { await vi.advanceTimersByTimeAsync(SETTLE_DELAY_MS * 2) })
    expect(getSession).not.toHaveBeenCalled()
  })
})
