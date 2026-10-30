// @vitest-environment jsdom
//
// The workspace controls: an owner or workspace superuser (`can_control`) gets the hold
// toggle and stop-everything for this workspace's tree; a member sees only the banner
// while it is on; an ancestor's hold shows here but is turned off up there.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { FleetStopOut, WorkspaceHoldOut } from '@/api/harness'

const getWorkspaceHold = vi.fn<(slug: string) => Promise<WorkspaceHoldOut>>()
const holdWorkspace = vi.fn<(slug: string, note?: string) => Promise<WorkspaceHoldOut>>()
const releaseWorkspaceHold = vi.fn<(slug: string) => Promise<WorkspaceHoldOut>>()
const stopWorkspace = vi.fn<(slug: string) => Promise<FleetStopOut>>()
vi.mock('@/api/harness', () => ({ getWorkspaceHold, holdWorkspace, releaseWorkspaceHold, stopWorkspace }))

const { WorkspaceControlsCard } = await import('./WorkspaceControlsCard')

function hold(overrides: Partial<WorkspaceHoldOut> = {}): WorkspaceHoldOut {
  return {
    workspace: 'dimagi', held: false, held_by_workspace: '', note: '', held_at: null,
    held_by_email: '', queued: 0, can_control: false, ...overrides,
  }
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('WorkspaceControlsCard', () => {
  it('shows a plain member nothing while the workspace runs', async () => {
    getWorkspaceHold.mockResolvedValue(hold())
    const { container } = render(<WorkspaceControlsCard slug="dimagi" name="Dimagi" />)
    await waitFor(() => expect(getWorkspaceHold).toHaveBeenCalled())
    expect(container.innerHTML).toBe('')
  })

  it('lets a superuser hold the workspace and stop everything in it', async () => {
    getWorkspaceHold.mockResolvedValue(hold({ can_control: true }))
    holdWorkspace.mockResolvedValue(hold({ can_control: true, held: true, note: 'audit' }))
    stopWorkspace.mockResolvedValue({ stopped_sessions: 1, stopped_turns: 0 })
    render(<WorkspaceControlsCard slug="dimagi" name="Dimagi" />)

    await waitFor(() => screen.getByTestId('workspace-controls-toggle'))
    fireEvent.change(screen.getByTestId('workspace-controls-note'), { target: { value: 'audit' } })
    fireEvent.click(screen.getByTestId('workspace-controls-toggle'))
    await waitFor(() => expect(screen.getByTestId('workspace-hold-banner')).toBeTruthy())
    expect(holdWorkspace).toHaveBeenCalledWith('dimagi', 'audit')

    fireEvent.click(screen.getByTestId('workspace-controls-stop-start'))
    fireEvent.click(screen.getByTestId('workspace-controls-stop-confirm'))
    await waitFor(() =>
      expect(screen.getByTestId('workspace-controls-stopped').textContent).toContain('Stopped 1 working session'),
    )
    expect(stopWorkspace).toHaveBeenCalledWith('dimagi')
  })

  it("shows an ancestor's hold, which is turned off up there", async () => {
    getWorkspaceHold.mockResolvedValue(hold({ workspace: 'connect', can_control: true, held: true, held_by_workspace: 'dimagi' }))
    render(<WorkspaceControlsCard slug="connect" name="Connect" />)
    await waitFor(() => screen.getByTestId('workspace-controls-toggle'))
    expect((screen.getByTestId('workspace-controls-toggle') as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByTestId('workspace-hold-banner').textContent).toContain('dimagi on hold')
  })
})
