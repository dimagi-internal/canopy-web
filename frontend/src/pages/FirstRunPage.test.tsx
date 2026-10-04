// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const listRequestableWorkspaces = vi.fn()
const requestWorkspaceAccess = vi.fn()
const createWorkspace = vi.fn()
vi.mock('@/api/workspaces', () => ({ listRequestableWorkspaces, requestWorkspaceAccess, createWorkspace }))

const refresh = vi.fn().mockResolvedValue(undefined)
vi.mock('@/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ workspaces: [], loading: false, refresh }),
}))
vi.mock('@/auth/AuthProvider', () => ({
  useAuth: () => ({ status: 'authenticated', user: { email: 'ada@dimagi.com', can_create_workspace: true } }),
}))

const { FirstRunPage } = await import('./FirstRunPage')

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<FirstRunPage />} />
        <Route path="/w/:workspace" element={<p>workspace home</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const dimagi = { slug: 'dimagi', display_name: 'Dimagi', domain: 'dimagi.com', pending_request_id: null }

describe('FirstRunPage — request an invitation', () => {
  it('offers "Request an invitation" instead of Join, and a pending request says so', async () => {
    listRequestableWorkspaces.mockResolvedValue([dimagi])
    requestWorkspaceAccess.mockResolvedValue({ id: 1, status: 'pending' })
    renderPage()
    const button = await screen.findByRole('button', { name: 'Request an invitation to Dimagi' })
    expect(screen.queryByRole('button', { name: /^join$/i })).toBeNull()
    fireEvent.change(screen.getByLabelText(/note to the admins of dimagi/i), { target: { value: 'hi' } })
    fireEvent.click(button)
    await waitFor(() => expect(requestWorkspaceAccess).toHaveBeenCalledWith('dimagi', 'hi'))
    expect(await screen.findByText(/an admin will review it/)).toBeTruthy()
  })

  it('an auto-approved request lands them in the workspace', async () => {
    listRequestableWorkspaces.mockResolvedValue([dimagi])
    requestWorkspaceAccess.mockResolvedValue({ id: 1, status: 'approved', role: 'editor' })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Request an invitation to Dimagi' }))
    expect(await screen.findByText('workspace home')).toBeTruthy()
    expect(refresh).toHaveBeenCalled()
  })

  it('an already-open request shows as requested on load', async () => {
    listRequestableWorkspaces.mockResolvedValue([{ ...dimagi, pending_request_id: 5 }])
    renderPage()
    expect(await screen.findByText(/an admin will review it/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /request an invitation/i })).toBeNull()
  })
})
