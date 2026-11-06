// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

const listAuthorizedApps = vi.fn()
const disconnectAuthorizedApp = vi.fn()
vi.mock('@/api/tokens', () => ({ listAuthorizedApps, disconnectAuthorizedApp }))

const { AuthorizedAppsPanel } = await import('./AuthorizedAppsPanel')

const DATA = {
  mcp_url: 'https://labs.example.com/canopy/api/mcp/',
  apps: [{ id: 7, client_name: 'Claude Code', connected_at: '2026-09-30T00:00:00Z', last_used_at: null }],
}

describe('AuthorizedAppsPanel', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('shows the MCP address, the Claude Code command and the authorized app', async () => {
    listAuthorizedApps.mockResolvedValue(DATA)
    render(<AuthorizedAppsPanel />)
    expect(await screen.findByRole('cell', { name: 'Claude Code' })).toBeTruthy()
    expect(screen.getByText(DATA.mcp_url)).toBeTruthy()
    expect(screen.getByText(`claude mcp add --transport http canopy ${DATA.mcp_url}`)).toBeTruthy()
  })

  it('disconnects only after confirming', async () => {
    listAuthorizedApps.mockResolvedValue(DATA)
    disconnectAuthorizedApp.mockResolvedValue(true)
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
    render(<AuthorizedAppsPanel />)
    const btn = await screen.findByText('Disconnect')
    await act(async () => {
      fireEvent.click(btn)
      await Promise.resolve()
    })
    expect(disconnectAuthorizedApp).not.toHaveBeenCalled()
    await act(async () => {
      fireEvent.click(btn)
      await Promise.resolve()
    })
    expect(disconnectAuthorizedApp).toHaveBeenCalledWith(7)
    confirmSpy.mockRestore()
  })
})
