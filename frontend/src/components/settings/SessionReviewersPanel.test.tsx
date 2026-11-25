// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const listMySessionReviewers = vi.fn()
const addMySessionReviewer = vi.fn()
const removeMySessionReviewer = vi.fn()
vi.mock('@/api/people', () => ({ listMySessionReviewers, addMySessionReviewer, removeMySessionReviewer }))

const { SessionReviewersPanel } = await import('./SessionReviewersPanel')

const trellis = { kind: 'agent', slug: 'trellis', name: 'Trellis', workspace: 'dimagi' }
const ada = { kind: 'agent', slug: 'ada', name: 'Ada', workspace: 'dimagi' }
const off = { on: false, since: null, coaches: [trellis, ada], reviewers: [], agents: [], coach: null }
const at = '2026-10-01T00:00:00Z'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('SessionReviewersPanel', () => {
  it('opts in to coaching, naming no agent, then opts out', async () => {
    listMySessionReviewers.mockResolvedValue(off)
    addMySessionReviewer.mockResolvedValue({ ...off, on: true, since: at })
    removeMySessionReviewer.mockResolvedValue(off)
    render(<SessionReviewersPanel />)
    const box = (await screen.findByRole('checkbox', { name: /Opt in to coaching/ })) as HTMLInputElement
    expect(box.checked).toBe(false)
    // Says who coaches now, what it does on the person's own machine, who else can read it.
    expect(screen.getByText('Who coaches you now')).toBeTruthy()
    expect(screen.getAllByText(/Ada/).length).toBeGreaterThan(0)
    expect(screen.getByText(/upload those sessions in full/)).toBeTruthy()
    expect(screen.getByText(/any member of its workspace can open it/)).toBeTruthy()
    expect(screen.getByText(/stay on canopy until you delete them/)).toBeTruthy()
    fireEvent.click(box)
    await waitFor(() => expect(addMySessionReviewer).toHaveBeenCalledWith())
    await waitFor(() => expect(box.checked).toBe(true))
    fireEvent.click(box)
    await waitFor(() => expect(removeMySessionReviewer).toHaveBeenCalledWith())
    await waitFor(() => expect(box.checked).toBe(false))
  })

  it('makes no claims about coaches the detailed copy was not checked for', async () => {
    listMySessionReviewers.mockResolvedValue({
      ...off, coaches: [{ kind: 'person', slug: '', name: 'A. Director', workspace: 'division' }],
    })
    render(<SessionReviewersPanel />)
    await screen.findByRole('checkbox', { name: /Opt in to coaching: A. Director/ })
    expect(screen.queryByText('What does this mean?')).toBeNull()
    expect(screen.queryByText(/emails me/)).toBeNull()
    expect(screen.getByText(/a person designated for division/)).toBeTruthy()
  })

  it('stays on screen, with the list, while opted in even if no coach is listed', async () => {
    listMySessionReviewers.mockResolvedValue({ ...off, on: true, since: at, coaches: [] })
    render(<SessionReviewersPanel />)
    const box = (await screen.findByRole('checkbox', { name: /Opt in to coaching/ })) as HTMLInputElement
    expect(box.checked).toBe(true)
  })

  it('shows nothing when nobody coaches you and you are not opted in', async () => {
    listMySessionReviewers.mockResolvedValue({ ...off, coaches: [] })
    const { container } = render(<SessionReviewersPanel />)
    await waitFor(() => expect(listMySessionReviewers).toHaveBeenCalled())
    expect(container.textContent).toBe('')
  })
})
