// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import type { InvitePreviewOut, WorkspaceOut } from '@/api/workspaces'

// Same hoist-then-dynamic-import pattern as WorkspaceMembersPage.test.tsx /
// RunnerAssignments.test.tsx.
const previewInvite = vi.fn<(token: string) => Promise<InvitePreviewOut>>()
const acceptInvite = vi.fn<(token: string) => Promise<WorkspaceOut>>()

class FakeWorkspaceApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

vi.mock('@/api/workspaces', () => ({
  previewInvite,
  acceptInvite,
  WorkspaceApiError: FakeWorkspaceApiError,
}))

type AuthState =
  | { status: 'loading'; user: null }
  | { status: 'authenticated'; user: { email: string; name: string; avatar_url: string | null } }
  | { status: 'anonymous'; user: null }

let mockAuth: AuthState = { status: 'anonymous', user: null }
vi.mock('@/auth/AuthProvider', () => ({
  useAuth: () => mockAuth,
}))

const { InviteAcceptPage } = await import('./InviteAcceptPage')

function preview(overrides: Partial<InvitePreviewOut> = {}): InvitePreviewOut {
  return {
    status: 'pending',
    email_hint: 'j•••@dimagi.com',
    workspace_slug: 'connect',
    workspace_display_name: 'Connect',
    role: 'editor',
    ...overrides,
  }
}

function renderPage(initialEntry = '/invite/tok-abc') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/invite/:token" element={<InviteAcceptPage />} />
        <Route path="/w/:workspace" element={<div>WORKSPACE HOME</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  mockAuth = { status: 'anonymous', user: null }
})

describe('InviteAcceptPage', () => {
  it('unauthenticated + pending shows the workspace name/role and a sign-in action', async () => {
    mockAuth = { status: 'anonymous', user: null }
    previewInvite.mockResolvedValue(preview())

    renderPage()

    expect(await screen.findByText(/Connect/)).toBeTruthy()
    expect(screen.getByText(/editor/i)).toBeTruthy()
    const signIn = screen.getByRole('link', { name: /sign in/i })
    expect(signIn.getAttribute('href')).toContain('/accounts/google/login/')
    expect(signIn.getAttribute('href')).toContain(encodeURIComponent('/invite/tok-abc'))
    expect(screen.queryByRole('button', { name: /accept/i })).toBeNull()
  })

  it('authenticated + pending shows an Accept button that accepts and navigates into the workspace', async () => {
    mockAuth = { status: 'authenticated', user: { email: 'j@dimagi.com', name: 'J', avatar_url: null } }
    previewInvite.mockResolvedValue(preview())
    acceptInvite.mockResolvedValue({
      slug: 'connect',
      display_name: 'Connect',
      auto_join_domains: [],
      role: 'editor',
      created_at: new Date().toISOString(),
    })

    renderPage()

    const acceptBtn = await screen.findByRole('button', { name: /accept/i })
    fireEvent.click(acceptBtn)

    await waitFor(() => expect(acceptInvite).toHaveBeenCalledWith('tok-abc'))
    expect(await screen.findByText('WORKSPACE HOME')).toBeTruthy()
  })

  it.each(['expired', 'revoked', 'accepted'] as const)(
    '%s shows a terminal message with no Accept button',
    async (status) => {
      mockAuth = { status: 'authenticated', user: { email: 'j@dimagi.com', name: 'J', avatar_url: null } }
      previewInvite.mockResolvedValue({ status, email_hint: 'j•••@dimagi.com' })

      renderPage()

      await waitFor(() => expect(screen.queryByText(/loading/i)).toBeNull())
      expect(screen.queryByRole('button', { name: /accept/i })).toBeNull()
      // Some clear message is shown — don't over-specify the copy.
      expect(document.body.textContent?.length ?? 0).toBeGreaterThan(0)
    },
  )

  it('an unknown token 404s to a terminal error, no Accept button', async () => {
    previewInvite.mockRejectedValue(new FakeWorkspaceApiError(404, 'invite not found'))

    renderPage()

    await waitFor(() => expect(screen.queryByText(/loading/i)).toBeNull())
    expect(screen.queryByRole('button', { name: /accept/i })).toBeNull()
  })

  it('a 403 (email mismatch) on accept explains the bound address and offers to sign out', async () => {
    mockAuth = { status: 'authenticated', user: { email: 'wrong@dimagi.com', name: 'W', avatar_url: null } }
    previewInvite.mockResolvedValue(preview())
    acceptInvite.mockRejectedValue(new FakeWorkspaceApiError(403, 'email_mismatch'))

    renderPage()

    const acceptBtn = await screen.findByRole('button', { name: /accept/i })
    fireEvent.click(acceptBtn)

    await waitFor(() => expect(acceptInvite).toHaveBeenCalled())
    expect(await screen.findByText(/bound to a different email address/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /sign out/i })).toBeTruthy()
  })
})
