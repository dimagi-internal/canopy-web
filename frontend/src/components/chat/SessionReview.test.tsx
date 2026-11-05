// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SessionReviewPanel } from './SessionReview'

const api = vi.hoisted(() => ({
  askMySessionReview: vi.fn(),
  getMySessionReview: vi.fn(),
  withdrawMySessionReview: vi.fn(),
}))
vi.mock('@/api/people', () => api)

const offer = { agent: 'trellis', name: 'Trellis', available: true, reason: '', note: '', ask: null }
const live = {
  ...offer,
  ask: { expires_at: '2026-11-11T12:00:00Z', conversation: 'c1', workspace: 'dimagi' },
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('SessionReviewPanel', () => {
  it('says what asking does and presses once', async () => {
    api.askMySessionReview.mockResolvedValue({ offer: live })
    const onChange = vi.fn()
    render(<SessionReviewPanel sessionId="s1" offer={offer} onChange={onChange} />)
    expect(screen.getByTestId('session-review-ask').textContent).toContain('does not turn coaching on')
    fireEvent.click(screen.getByTestId('session-review-press'))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(live))
    expect(api.askMySessionReview).toHaveBeenCalledTimes(1)
  })

  it('says plainly why it cannot be asked, with no button', () => {
    const reason = 'canopy does not hold this session\'s messages, so Trellis would have nothing to read.'
    render(<SessionReviewPanel sessionId="s1" offer={{ ...offer, available: false, reason }} onChange={vi.fn()} />)
    expect(screen.getByTestId('session-review-reason').textContent).toBe(reason)
    expect(screen.queryByTestId('session-review-press')).toBeNull()
  })

  it('shows what canopy holds before the press', () => {
    render(<SessionReviewPanel sessionId="s1" offer={{ ...offer, note: 'From message 448 on.' }} onChange={vi.fn()} />)
    expect(screen.getByTestId('session-review-note').textContent).toBe('From message 448 on.')
  })

  it('links the reply and withdraws a live ask', async () => {
    api.withdrawMySessionReview.mockResolvedValue({ offer })
    const onChange = vi.fn()
    render(<SessionReviewPanel sessionId="s1" offer={live} onChange={onChange} />)
    expect(screen.getByRole('link').getAttribute('href')).toContain('/w/dimagi/sessions/c1')
    expect(screen.getByTestId('session-review-withdraw-says').textContent).toContain("can't make")
    fireEvent.click(screen.getByTestId('session-review-withdraw'))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(offer))
  })
})
