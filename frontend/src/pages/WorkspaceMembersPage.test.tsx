// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import type { MemberOut, InviteOut } from '@/api/workspaces'

// vi.mock is hoisted above these declarations, but the factories aren't invoked
// until the dynamic `import('./WorkspaceMembersPage')` below actually pulls in
// '@/api/workspaces' / '@/workspace/WorkspaceProvider' — same pattern as
// RunnerAssignments.test.tsx / ChatSessionsPanel.test.tsx.
const listMembers = vi.fn<(slug: string) => Promise<MemberOut[]>>()
const removeMember = vi.fn<(slug: string, userId: number) => Promise<void>>()
const listInvites = vi.fn<(slug: string) => Promise<InviteOut[]>>()
const createInvite = vi.fn<(slug: string, email: string, role: string) => Promise<InviteOut>>()
const revokeInvite = vi.fn<(slug: string, inviteId: number) => Promise<void>>()

class FakeWorkspaceApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

vi.mock('@/api/workspaces', () => ({
  listMembers,
  removeMember,
  listInvites,
  createInvite,
  revokeInvite,
  WorkspaceApiError: FakeWorkspaceApiError,
}))

let mockRole: string | undefined = 'owner'
vi.mock('@/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    workspaces: mockRole ? [{ slug: 'acme', display_name: 'Acme', role: mockRole }] : [],
    active: 'acme',
    loading: false,
  }),
}))

const { WorkspaceMembersPage } = await import('./WorkspaceMembersPage')

function member(overrides: Partial<MemberOut> = {}): MemberOut {
  return {
    user_id: 1,
    email: 'alice@dimagi.com',
    role: 'owner',
    joined_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function invite(overrides: Partial<InviteOut> = {}): InviteOut {
  return {
    id: 1,
    email: 'bob@example.com',
    role: 'editor',
    token: 'tok-123',
    expires_at: new Date(Date.now() + 86_400_000).toISOString(),
    accepted_at: null,
    revoked_at: null,
    ...overrides,
  }
}

function renderPage(initialEntry = '/w/acme/members') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/w/:workspace/members" element={<WorkspaceMembersPage />} />
        <Route path="/" element={<div>HOME</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  mockRole = 'owner'
})

describe('WorkspaceMembersPage', () => {
  it('lists members with roles and pending invites', async () => {
    listMembers.mockResolvedValue([
      member({ user_id: 1, email: 'alice@dimagi.com', role: 'owner' }),
      member({ user_id: 2, email: 'carol@dimagi.com', role: 'viewer' }),
    ])
    listInvites.mockResolvedValue([invite({ email: 'bob@example.com', role: 'editor' })])

    renderPage()

    expect(await screen.findByText('alice@dimagi.com')).toBeTruthy()
    expect(screen.getByText('carol@dimagi.com')).toBeTruthy()
    expect(screen.getByText('bob@example.com')).toBeTruthy()
  })

  it('does not show an accepted or revoked invite as pending', async () => {
    listMembers.mockResolvedValue([member()])
    listInvites.mockResolvedValue([
      invite({ id: 2, email: 'accepted@example.com', accepted_at: new Date().toISOString() }),
      invite({ id: 3, email: 'revoked@example.com', revoked_at: new Date().toISOString() }),
      invite({ id: 4, email: 'expired@example.com', expires_at: new Date(Date.now() - 1000).toISOString() }),
      invite({ id: 5, email: 'pending@example.com' }),
    ])

    renderPage()

    expect(await screen.findByText('pending@example.com')).toBeTruthy()
    expect(screen.queryByText('accepted@example.com')).toBeNull()
    expect(screen.queryByText('revoked@example.com')).toBeNull()
    expect(screen.queryByText('expired@example.com')).toBeNull()
  })

  it('an owner sees the invite form and revoke/remove controls', async () => {
    mockRole = 'owner'
    listMembers.mockResolvedValue([member()])
    listInvites.mockResolvedValue([invite()])

    renderPage()

    await screen.findByText('bob@example.com')
    expect(screen.getByLabelText(/remove alice@dimagi.com/i)).toBeTruthy()
    expect(screen.getByLabelText(/revoke invite to bob@example.com/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /create invite/i })).toBeTruthy()
  })

  it('a non-owner sees neither the invite form nor revoke/remove controls', async () => {
    mockRole = 'editor'
    listMembers.mockResolvedValue([member()])
    listInvites.mockResolvedValue([invite()])

    renderPage()

    await screen.findByText('bob@example.com')
    expect(screen.queryByLabelText(/remove alice@dimagi.com/i)).toBeNull()
    expect(screen.queryByLabelText(/revoke invite to bob@example.com/i)).toBeNull()
    expect(screen.queryByRole('button', { name: /create invite/i })).toBeNull()
  })

  it('creating an invite renders the resulting link with a copy button and a no-email note', async () => {
    mockRole = 'owner'
    listMembers.mockResolvedValue([member()])
    listInvites.mockResolvedValue([])
    createInvite.mockResolvedValue(invite({ id: 9, email: 'newperson@example.com', token: 'brandnew-token' }))

    renderPage()
    await screen.findByText('alice@dimagi.com')

    fireEvent.change(screen.getByLabelText(/email/i), { target: { value: 'newperson@example.com' } })
    fireEvent.click(screen.getByRole('button', { name: /create invite/i }))

    await waitFor(() => expect(createInvite).toHaveBeenCalledWith('acme', 'newperson@example.com', 'editor'))

    expect(await screen.findByText(/brandnew-token/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /copy link/i })).toBeTruthy()
    expect(screen.getByText(/canopy does not/i)).toBeTruthy()
    expect(screen.getByText(/send this link to them yourself/i)).toBeTruthy()
  })

  it('revoke removes the invite row', async () => {
    mockRole = 'owner'
    listMembers.mockResolvedValue([member()])
    listInvites.mockResolvedValue([invite({ id: 7, email: 'gone-soon@example.com' })])
    revokeInvite.mockResolvedValue(undefined)

    renderPage()
    await screen.findByText('gone-soon@example.com')

    fireEvent.click(screen.getByLabelText(/revoke invite to gone-soon@example.com/i))

    await waitFor(() => expect(revokeInvite).toHaveBeenCalledWith('acme', 7))
    await waitFor(() => expect(screen.queryByText('gone-soon@example.com')).toBeNull())
  })

  it('removing a member removes the row', async () => {
    mockRole = 'owner'
    listMembers.mockResolvedValue([member({ user_id: 5, email: 'leaving@dimagi.com' })])
    listInvites.mockResolvedValue([])
    removeMember.mockResolvedValue(undefined)

    renderPage()
    await screen.findByText('leaving@dimagi.com')

    fireEvent.click(screen.getByLabelText(/remove leaving@dimagi.com/i))

    await waitFor(() => expect(removeMember).toHaveBeenCalledWith('acme', 5))
    await waitFor(() => expect(screen.queryByText('leaving@dimagi.com')).toBeNull())
  })

  it('redirects to the workspace home on a 404 (non-member)', async () => {
    listMembers.mockRejectedValue(new FakeWorkspaceApiError(404, 'workspace not found'))
    listInvites.mockResolvedValue([])

    renderPage()

    expect(await screen.findByText('HOME')).toBeTruthy()
  })
})
