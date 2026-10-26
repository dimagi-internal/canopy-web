// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const listRequestableWorkspaces = vi.fn()
const requestWorkspaceAccess = vi.fn()
const createWorkspace = vi.fn()
vi.mock('@/api/workspaces', () => ({ listRequestableWorkspaces, requestWorkspaceAccess, createWorkspace }))

const listMySessionReviewers = vi.fn().mockResolvedValue({ reviewers: [], agents: [], coach: null })
const addMySessionReviewer = vi.fn().mockResolvedValue({ reviewers: [], agents: [], coach: null })
const removeMySessionReviewer = vi.fn()
vi.mock('@/api/people', () => ({ listMySessionReviewers, addMySessionReviewer, removeMySessionReviewer }))

const refresh = vi.fn().mockResolvedValue(undefined)
const wsState = vi.hoisted(() => ({
  value: { workspaces: [] as { slug: string; display_name: string }[], active: null as string | null },
}))
vi.mock('@/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ ...wsState.value, loading: false, refresh }),
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
  listMySessionReviewers.mockResolvedValue({ reviewers: [], agents: [], coach: null })
  wsState.value = { workspaces: [], active: null }
})

describe('FirstRunPage — create an agent', () => {
  function renderNewWorkspace() {
    return render(
      <MemoryRouter initialEntries={['/new-workspace']}>
        <Routes>
          <Route path="/new-workspace" element={<FirstRunPage alwaysOfferForm />} />
        </Routes>
      </MemoryRouter>,
    )
  }

  it('a member links to creating an agent in the workspace they are in', async () => {
    listRequestableWorkspaces.mockResolvedValue([])
    wsState.value = { workspaces: [{ slug: 'marshellis', display_name: 'Marshellis' }], active: 'marshellis' }
    renderNewWorkspace()
    const link = await screen.findByRole('link', { name: 'Create an agent' })
    expect(link.getAttribute('href')).toBe('/w/marshellis/agents/new')
    expect(screen.getByText(/in Marshellis\./)).toBeTruthy()
  })

  it('someone in no workspace yet has nowhere to create one, so no link', async () => {
    listRequestableWorkspaces.mockResolvedValue([])
    renderPage()
    await screen.findByRole('button', { name: 'Create workspace' })
    expect(screen.queryByRole('link', { name: 'Create an agent' })).toBeNull()
  })
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

const trellis = { agent: 'trellis', name: 'Trellis', workspace: 'dimagi', member: false, on: false }

describe('FirstRunPage — coaching opt-in', () => {
  it('no coach offered: no checkbox', async () => {
    listRequestableWorkspaces.mockResolvedValue([dimagi])
    renderPage()
    await screen.findByRole('button', { name: 'Request an invitation to Dimagi' })
    expect(screen.queryByRole('checkbox', { name: /coach me/i })).toBeNull()
  })

  it('ticked on an auto-approved request, it lets the coach review their sessions', async () => {
    listRequestableWorkspaces.mockResolvedValue([dimagi])
    listMySessionReviewers.mockResolvedValue({ reviewers: [], agents: [], coach: trellis })
    requestWorkspaceAccess.mockResolvedValue({ id: 1, status: 'approved', role: 'editor' })
    renderPage()
    const box = await screen.findByRole('checkbox', { name: /Have Trellis coach me/ })
    expect((box as HTMLInputElement).checked).toBe(false) // opt-in, never pre-ticked
    expect(screen.getByText(/Your computer will upload your synced Claude Code sessions to canopy so Trellis/)).toBeTruthy()
    fireEvent.click(box)
    fireEvent.click(screen.getByRole('button', { name: 'Request an invitation to Dimagi' }))
    expect(await screen.findByText('workspace home')).toBeTruthy()
    expect(addMySessionReviewer).toHaveBeenCalledWith('trellis')
  })

  it('left unticked, nothing is granted', async () => {
    listRequestableWorkspaces.mockResolvedValue([dimagi])
    listMySessionReviewers.mockResolvedValue({ reviewers: [], agents: [], coach: trellis })
    requestWorkspaceAccess.mockResolvedValue({ id: 1, status: 'approved', role: 'editor' })
    renderPage()
    await screen.findByRole('checkbox', { name: /Have Trellis coach me/ })
    fireEvent.click(screen.getByRole('button', { name: 'Request an invitation to Dimagi' }))
    expect(await screen.findByText('workspace home')).toBeTruthy()
    expect(addMySessionReviewer).not.toHaveBeenCalled()
  })

  it('ticked on a pending request, it grants nothing yet and says where to turn it on', async () => {
    listRequestableWorkspaces.mockResolvedValue([dimagi])
    listMySessionReviewers.mockResolvedValue({ reviewers: [], agents: [], coach: trellis })
    requestWorkspaceAccess.mockResolvedValue({ id: 1, status: 'pending' })
    renderPage()
    fireEvent.click(await screen.findByRole('checkbox', { name: /Have Trellis coach me/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Request an invitation to Dimagi' }))
    expect(await screen.findByText(/turn on Trellis's coaching from Settings/)).toBeTruthy()
    expect(addMySessionReviewer).not.toHaveBeenCalled()
  })
})
