// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

const getSessionSharing = vi.fn()
const setSessionSharing = vi.fn()
vi.mock('@/api/canopyProjects', () => ({ getSessionSharing, setSessionSharing }))

const { SessionSharingPanel } = await import('./SessionSharingPanel')

const row = {
  project_id: 7, handle: 'supply', name: 'Supply', workspace: 'dimagi', repo: 'connect-labs',
  shared: true, team_size: 3,
}

describe('SessionSharingPanel', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('is not there for someone who shares nothing', async () => {
    getSessionSharing.mockResolvedValue([])
    const { container } = render(<SessionSharingPanel />)
    await act(async () => { await Promise.resolve() })
    expect(container.innerHTML).toBe('')
  })

  it('lets someone who still shares stop, and never offers to start', async () => {
    getSessionSharing.mockResolvedValue([row])
    setSessionSharing.mockResolvedValue({ ...row, shared: false })
    render(<SessionSharingPanel />)
    const stop = await screen.findByTestId('session-sharing-stop-7')
    expect(screen.getByText(/3 people today/)).toBeTruthy()
    expect(screen.queryByRole('checkbox')).toBeNull()
    await act(async () => {
      fireEvent.click(stop)
      await Promise.resolve()
    })
    expect(setSessionSharing).toHaveBeenCalledWith(7, false)
    expect(screen.queryByTestId('settings-session-sharing')).toBeNull()
  })
})
