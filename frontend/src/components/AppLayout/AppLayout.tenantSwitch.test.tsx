// @vitest-environment jsdom
// Switching workspace must reload the page's data.
//
// React Router reuses a route's element when only a PARAM changes, so
// /w/strategy/agents → /w/connect/agents kept AgentsPage mounted, and its
// fetch-on-mount never ran again: the header said "Connect" while the list
// still showed Strategy's agents (here, none — "No agents in this workspace").
// AppLayout keys its <Outlet> on :workspace so EVERY tenant page remounts.
// This drives the real AgentsPage through the real shell, not a stand-in.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import type { WorkspaceOut } from '@/api/workspaces'
import type { AgentOut } from '@/api/agents'

const listWorkspaces = vi.fn<() => Promise<WorkspaceOut[]>>()
vi.mock('@/api/workspaces', () => ({ listWorkspaces }))

// The client pins the tenant from the URL (client.v2's rewrite), so answer by
// the URL the page is on when it asks — the same thing the server keys on.
let currentWorkspace = ''
const AGENTS: Record<string, string[]> = { strategy: [], connect: ['ace', 'hal'] }
const listAgents = vi.fn(async () => ({
  items: (AGENTS[currentWorkspace] ?? []).map(
    (slug, id) => ({ id, slug, name: slug.toUpperCase() }) as unknown as AgentOut,
  ),
  count: 0,
}))
vi.mock('@/api/agents', () => ({ listAgents }))

const { AppLayout } = await import('./AppLayout')
const { AgentsPage } = await import('@/pages/AgentsPage')
const { AuthContext } = await import('@/auth/AuthProvider')
const { ThemeProvider } = await import('@/theme/ThemeProvider')

describe('AppLayout — switching workspace', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('reloads the Agents list for the new workspace', async () => {
    listWorkspaces.mockResolvedValue([
      { slug: 'strategy', display_name: 'Strategy' },
      { slug: 'connect', display_name: 'Connect' },
    ] as WorkspaceOut[])
    const router = createMemoryRouter(
      [{ element: <AppLayout />, children: [{ path: '/w/:workspace/agents', element: <AgentsPage /> }] }],
      { initialEntries: ['/w/strategy/agents'] },
    )
    router.subscribe((s) => {
      currentWorkspace = s.location.pathname.split('/')[2] ?? ''
    })
    currentWorkspace = 'strategy'
    render(
      <ThemeProvider>
        <AuthContext.Provider
          value={{
            status: 'authenticated',
            user: { name: 'J', email: 'jj@dimagi.com', avatar_url: '', can_create_workspace: true },
          }}
        >
          <RouterProvider router={router} />
        </AuthContext.Provider>
      </ThemeProvider>,
    )
    expect(await screen.findByText('No agents in this workspace')).toBeTruthy()

    await act(async () => {
      await router.navigate('/w/connect/agents')
    })

    expect(await screen.findByText('ACE')).toBeTruthy()
    expect(screen.getByText('HAL')).toBeTruthy()
    expect(screen.queryByText('No agents in this workspace')).toBeNull()
    expect(listAgents).toHaveBeenCalledTimes(2)
  })
})
