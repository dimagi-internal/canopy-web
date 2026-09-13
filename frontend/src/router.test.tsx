// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { WorkspaceOut } from '@/api/workspaces'

// vi.mock is hoisted above these declarations by vitest's transform, but the
// factory isn't *called* until the mocked modules are actually imported —
// which happens on the dynamic imports below. Same pattern as
// SettingsPage.presence.test.tsx / AppLayout.test.tsx.
const listWorkspaces = vi.fn<() => Promise<WorkspaceOut[]>>()
// FirstRunPage (rendered by both RootRedirect and TenantRedirect in the
// zero-workspace case this file pins) fetches the self-join capability list
// on mount — an unmocked call throws inside the effect and fails the test
// with an unhandled rejection even though the assertions themselves pass.
const listJoinableWorkspaces = vi.fn().mockResolvedValue([])
vi.mock('@/api/workspaces', () => ({ listWorkspaces, listJoinableWorkspaces }))

const { RootRedirect, TenantRedirect } = await import('./router')
const { AuthContext } = await import('@/auth/AuthProvider')
const { WorkspaceProvider } = await import('@/workspace/WorkspaceProvider')

/**
 * Pins the branch's headline fix: a zero-workspace user used to render `null`
 * at both `RootRedirect` (bare "/") and `TenantRedirect` (legacy flat tenant
 * paths). Revert either back to `return null` and this test fails — see the
 * fix-wave report for the revert-and-observe evidence.
 */
function renderWithEmptyWorkspaces(node: React.ReactElement) {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <AuthContext.Provider
        value={{
          status: 'authenticated',
          user: {
            name: 'Jonathan Jackson',
            email: 'jj@dimagi.com',
            avatar_url: '',
            can_create_workspace: true,
          },
        }}
      >
        <WorkspaceProvider urlSlug={null}>{node}</WorkspaceProvider>
      </AuthContext.Provider>
    </MemoryRouter>,
  )
}

describe('the first-run screen renders for a zero-workspace user', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('RootRedirect ("/") renders FirstRunPage rather than a blank page', async () => {
    listWorkspaces.mockResolvedValue([])

    renderWithEmptyWorkspaces(<RootRedirect />)

    expect(await screen.findByText('Welcome to Canopy')).toBeTruthy()
  })

  it('TenantRedirect (legacy flat tenant path) renders FirstRunPage rather than a blank page', async () => {
    listWorkspaces.mockResolvedValue([])

    renderWithEmptyWorkspaces(<TenantRedirect to="agents" />)

    expect(await screen.findByText('Welcome to Canopy')).toBeTruthy()
  })
})
