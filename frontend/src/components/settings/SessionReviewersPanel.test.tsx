// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const listMySessionReviewers = vi.fn()
const addMySessionReviewer = vi.fn()
const removeMySessionReviewer = vi.fn()
vi.mock('@/api/people', () => ({ listMySessionReviewers, addMySessionReviewer, removeMySessionReviewer }))

const { SessionReviewersPanel } = await import('./SessionReviewersPanel')

const coach = { agent: 'trellis', name: 'Trellis', workspace: 'dimagi', member: true, on: false }
const at = '2026-10-01T00:00:00Z'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('SessionReviewersPanel', () => {
  it('turns the coach on, then off', async () => {
    listMySessionReviewers.mockResolvedValue({ reviewers: [], agents: [], coach })
    addMySessionReviewer.mockResolvedValue({
      reviewers: [{ agent: 'trellis', name: 'Trellis', granted_at: at }], agents: [], coach: { ...coach, on: true },
    })
    removeMySessionReviewer.mockResolvedValue({ reviewers: [], agents: [], coach })
    render(<SessionReviewersPanel />)
    const box = (await screen.findByRole('checkbox', { name: /Have Trellis coach me/ })) as HTMLInputElement
    expect(box.checked).toBe(false)
    fireEvent.click(box)
    await waitFor(() => expect(addMySessionReviewer).toHaveBeenCalledWith('trellis'))
    await waitFor(() => expect(box.checked).toBe(true))
    fireEvent.click(box)
    await waitFor(() => expect(removeMySessionReviewer).toHaveBeenCalledWith('trellis'))
    await waitFor(() => expect(box.checked).toBe(false))
  })

  it('lists another reviewer with a Stop', async () => {
    listMySessionReviewers.mockResolvedValue({
      reviewers: [{ agent: 'hal', name: 'Hal', granted_at: at }], agents: [], coach: null,
    })
    removeMySessionReviewer.mockResolvedValue({ reviewers: [], agents: [], coach: null })
    render(<SessionReviewersPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(removeMySessionReviewer).toHaveBeenCalledWith('hal'))
  })

  it('shows nothing when there is no coach and no reviewer', async () => {
    listMySessionReviewers.mockResolvedValue({ reviewers: [], agents: [], coach: null })
    const { container } = render(<SessionReviewersPanel />)
    await waitFor(() => expect(listMySessionReviewers).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })
})
