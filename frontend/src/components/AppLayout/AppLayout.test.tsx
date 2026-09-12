// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { AiStatusLegacy } from '@/api/ai'
import type { WorkspaceOut } from '@/api/workspaces'

// vi.mock is hoisted above these declarations by vitest's transform, but the
// factory isn't *called* until the mocked modules are actually imported —
// which happens on the dynamic imports below. Same pattern as
// SettingsPage.presence.test.tsx / RunnerAssignments.test.tsx.

const listWorkspaces = vi.fn<() => Promise<WorkspaceOut[]>>()
vi.mock('@/api/workspaces', () => ({ listWorkspaces }))

const aiStatus = vi.fn<() => Promise<AiStatusLegacy>>()
const aiSwitch = vi.fn()
vi.mock('@/api/ai', () => ({ aiStatus, aiSwitch }))

const { AppLayout } = await import('./AppLayout')
const { AuthContext } = await import('@/auth/AuthProvider')
const { ThemeProvider } = await import('@/theme/ThemeProvider')

function renderAs(status: 'authenticated' | 'anonymous') {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <ThemeProvider>
        <AuthContext.Provider
          value={
            status === 'authenticated'
              ? {
                  status,
                  user: {
                    name: 'Jonathan Jackson',
                    email: 'jj@dimagi.com',
                    avatar_url: '',
                    can_create_workspace: true,
                  },
                }
              : { status, user: null }
          }
        >
          <AppLayout />
        </AuthContext.Provider>
      </ThemeProvider>
    </MemoryRouter>,
  )
}

describe('AppLayout — WorkspaceSwitcher gating', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('hides "+ Workspace" from an anonymous visitor (public link routes reach this shell)', async () => {
    listWorkspaces.mockResolvedValue([])
    // UserMenu's aiStatus poll runs unconditionally in an effect, before its
    // own auth check — mock it even though this test is the anonymous case.
    aiStatus.mockResolvedValue({ backend: 'api', ready: true, detail: 'ok', setup_hint: null })

    renderAs('anonymous')

    // Let listWorkspaces' promise settle.
    await Promise.resolve()
    await Promise.resolve()

    expect(screen.queryByText('+ Workspace')).toBeNull()
  })

  it('shows "+ Workspace" to an authenticated user', async () => {
    listWorkspaces.mockResolvedValue([])
    aiStatus.mockResolvedValue({ backend: 'api', ready: true, detail: 'ok', setup_hint: null })

    renderAs('authenticated')

    expect(await screen.findByText('+ Workspace')).toBeTruthy()
  })
})
