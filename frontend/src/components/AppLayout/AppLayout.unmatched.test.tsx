// @vitest-environment jsdom
// An address no route matches still names its workspace. /w/marshellis/topology
// (a hand-written link, 2026-10-18) matched nothing, so there was no :workspace
// param and the header showed the last workspace used — "Dimagi" — on a page
// about marshellis. The header reads the workspace off the address itself.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import type { WorkspaceOut } from '@/api/workspaces'

const listWorkspaces = vi.fn<() => Promise<WorkspaceOut[]>>()
vi.mock('@/api/workspaces', () => ({ listWorkspaces }))

const { AppLayout } = await import('./AppLayout')
const { AuthContext } = await import('@/auth/AuthProvider')
const { ThemeProvider } = await import('@/theme/ThemeProvider')

describe('AppLayout — an unmatched /w/<slug>/… address', () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('shows the workspace in the address, not the last one used', async () => {
    // The workspace this browser last had in the address.
    vi.stubGlobal('localStorage', { getItem: () => 'dimagi', setItem: () => {} })
    listWorkspaces.mockResolvedValue([
      { slug: 'dimagi', display_name: 'Dimagi' },
      { slug: 'marshellis', display_name: 'Marshellis' },
    ] as WorkspaceOut[])
    const router = createMemoryRouter(
      [{ element: <AppLayout />, children: [{ path: '*', element: <p>not found</p> }] }],
      { initialEntries: ['/w/marshellis/nowhere'] },
    )
    render(
      <ThemeProvider>
        <AuthContext.Provider
          value={{
            status: 'authenticated',
            user: { name: 'J', email: 'jj@dimagi.com', avatar_url: '', can_create_workspace: true, memory_enabled: false, memory_paused: false },
          }}
        >
          <RouterProvider router={router} />
        </AuthContext.Provider>
      </ThemeProvider>,
    )
    await waitFor(() => {
      const picker = screen.getAllByRole('combobox')[0] as HTMLSelectElement
      expect(picker.value).toBe('marshellis')
    })
  })
})
