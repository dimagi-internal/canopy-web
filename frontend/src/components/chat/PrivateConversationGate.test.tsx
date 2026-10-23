// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const getSession = vi.fn()
const askToJoin = vi.fn()
vi.mock('@/api/chat', () => ({
  getSession: (...a: unknown[]) => getSession(...a),
  askToJoin: (...a: unknown[]) => askToJoin(...a),
}))
vi.mock('@/api/agents', () => ({ getAgent: async () => ({ name: 'Labs Data' }) }))

const { ANSWER_POLL_MS, ANSWER_POLL_MAX_MS, PrivateConversationGate, takeGateFetch } = await import('./PrivateConversationGate')

const PRIVATE = {
  id: 's1', sensitive: true, agent_slug: 'labsdata', started_by_name: 'Jonathan Jackson',
  created_by: 'jj@dimagi.com', your_join_request: '',
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  sessionStorage.clear()
})

const gate = () => render(
  <PrivateConversationGate sessionId="s1"><p>THE CHAT</p></PrivateConversationGate>)

describe('PrivateConversationGate', () => {
  it('lets an ordinary conversation straight through, and hands the chat page its read', async () => {
    getSession.mockResolvedValue({ id: 's1', sensitive: false })
    gate()
    await screen.findByText('THE CHAT')
    expect(await takeGateFetch('s1')).toMatchObject({ sensitive: false })
    expect(takeGateFetch('s1')).toBeNull()
  })

  it("asks one of its people to open it on purpose, then remembers it in this tab", async () => {
    getSession.mockResolvedValue({ ...PRIVATE, you_are_in_it: true })
    gate()
    const text = await screen.findByTestId('private-interstitial-text')
    await waitFor(() => expect(text.textContent).toBe(
      'This is a private conversation with Labs Data, started by Jonathan Jackson. '
      + 'Its messages are sealed to the people in it.'))
    expect(screen.queryByText('THE CHAT')).toBeNull()
    fireEvent.click(screen.getByTestId('private-open'))
    await screen.findByText('THE CHAT')
    cleanup()
    gate()
    await screen.findByText('THE CHAT')
  })

  it('offers someone not in it only a request to be added', async () => {
    getSession.mockResolvedValue({ ...PRIVATE, you_are_in_it: false })
    askToJoin.mockResolvedValue({ status: 'pending' })
    gate()
    expect((await screen.findByTestId('private-interstitial-text')).textContent).toBe(
      'This is a private conversation. Only the people in it can read it.')
    expect(screen.queryByTestId('private-open')).toBeNull()
    fireEvent.click(screen.getByText('Ask Jonathan Jackson to add you'))
    await screen.findByTestId('private-asked')
    expect(askToJoin).toHaveBeenCalledWith('s1')
    expect(screen.queryByText('THE CHAT')).toBeNull()
  })

  it('says so when they already asked', async () => {
    getSession.mockResolvedValue({ ...PRIVATE, you_are_in_it: false, your_join_request: 'pending' })
    gate()
    expect((await screen.findByTestId('private-asked')).textContent).toContain('Asked Jonathan Jackson')
    expect(screen.queryByTestId('private-ask')).toBeNull()
  })

  it('turns into Open it once its owner adds them, without a reload', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      getSession.mockResolvedValue({ ...PRIVATE, you_are_in_it: false, your_join_request: 'pending' })
      gate()
      await screen.findByTestId('private-asked')
      getSession.mockResolvedValue({ ...PRIVATE, you_are_in_it: true, your_join_request: 'approved' })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ANSWER_POLL_MS + 10)
      })
      expect((await screen.findByTestId('private-interstitial-text')).textContent)
        .toContain('Jonathan Jackson added you.')
      fireEvent.click(screen.getByTestId('private-open'))
      await screen.findByText('THE CHAT')
    } finally {
      vi.useRealTimers()
    }
  })

  it('offers to ask again once its owner declines', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      getSession.mockResolvedValue({ ...PRIVATE, you_are_in_it: false, your_join_request: 'pending' })
      gate()
      await screen.findByTestId('private-asked')
      getSession.mockResolvedValue({ ...PRIVATE, you_are_in_it: false, your_join_request: 'declined' })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ANSWER_POLL_MS + 10)
      })
      expect(await screen.findByText('Jonathan Jackson declined your last request.')).toBeTruthy()
      expect(screen.getByTestId('private-ask')).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('asks less often the longer it waits, and stops once the conversation is out of reach', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      getSession.mockResolvedValue({ ...PRIVATE, you_are_in_it: false, your_join_request: 'pending' })
      gate()
      await screen.findByTestId('private-asked')
      const first = getSession.mock.calls.length
      const after = async (ms: number) => {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(ms)
        })
        return getSession.mock.calls.length - first
      }
      expect(await after(ANSWER_POLL_MS + 10)).toBe(1)
      // The next wait is twice as long: nothing at one more interval.
      expect(await after(ANSWER_POLL_MS)).toBe(1)
      expect(await after(ANSWER_POLL_MS + 10)).toBe(2)
      // Never longer than the cap.
      await after(ANSWER_POLL_MAX_MS * 3)
      const capped = getSession.mock.calls.length
      expect(await after(ANSWER_POLL_MAX_MS + 10)).toBe(capped - first + 1)
      // A 404 (taken away, or never theirs): it says so and stops asking.
      getSession.mockRejectedValue(Object.assign(new Error('not found'), { status: 404 }))
      await after(ANSWER_POLL_MAX_MS + 10)
      expect(screen.getByTestId('private-gone')).toBeTruthy()
      const stopped = getSession.mock.calls.length
      await after(ANSWER_POLL_MAX_MS * 4)
      expect(getSession.mock.calls.length).toBe(stopped)
    } finally {
      vi.useRealTimers()
    }
  })
})
