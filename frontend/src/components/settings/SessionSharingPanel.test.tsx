// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

const getSessionSharing = vi.fn()
const setSessionSharing = vi.fn()
vi.mock('@/api/canopyProjects', () => ({ getSessionSharing, setSessionSharing }))

const { SessionSharingPanel } = await import('./SessionSharingPanel')

describe('SessionSharingPanel', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('shares your sessions with a project team only when you tick it', async () => {
    const row = {
      project_id: 7, handle: 'supply', name: 'Supply', workspace: 'dimagi', repo: 'connect-labs',
      shared: false, team_size: 3,
    }
    getSessionSharing.mockResolvedValue([row])
    setSessionSharing.mockResolvedValue({ ...row, shared: true })
    render(<SessionSharingPanel />)
    const box = (await screen.findByTestId('session-sharing-7')) as HTMLInputElement
    expect(box.checked).toBe(false)
    expect(screen.getByText(/team of 3/)).toBeTruthy()
    await act(async () => {
      fireEvent.click(box)
      await Promise.resolve()
    })
    expect(setSessionSharing).toHaveBeenCalledWith(7, true)
    expect(((await screen.findByTestId('session-sharing-7')) as HTMLInputElement).checked).toBe(true)
  })
})
