// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const listWorkspaceProjects = vi.fn()
const getWorkspaceProject = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  listWorkspaceProjects: (...a: unknown[]) => listWorkspaceProjects(...a),
  getWorkspaceProject: (...a: unknown[]) => getWorkspaceProject(...a),
}))

const { WorkspaceProjectLine, WorkspaceProjectRedirect, WorkspaceProjectsSection } = await import('./WorkspaceProjects')
const { AgentApiError } = await import('@/api/agents')

afterEach(() => {
  cleanup()
  listWorkspaceProjects.mockReset()
  getWorkspaceProject.mockReset()
})

const kb = {
  id: 1, agent_slug: 'eva', workspace: 'dimagi', scope: 'workspace', handle: 'givewell-kb', steward_slug: 'eva',
  address: 'dimagi/givewell-kb', url: '/w/dimagi/projects/givewell-kb', ext_id: 'P12', name: 'GiveWell Knowledge Base',
  outcome: 'Everything GiveWell publishes, cited', status: 'active', task_count: 3, open_task_count: 1,
} as never

describe('WorkspaceProjectsSection', () => {
  it("lists this workspace's projects with address and steward", async () => {
    listWorkspaceProjects.mockResolvedValue([kb, { ...(kb as object), id: 2, workspace: 'connect', name: 'Elsewhere' }])
    render(<MemoryRouter><WorkspaceProjectsSection workspace="dimagi" /></MemoryRouter>)

    expect(await screen.findByText('GiveWell Knowledge Base')).toBeTruthy()
    expect(screen.getByText('dimagi/givewell-kb')).toBeTruthy()
    expect(screen.getByText(/Steward eva · 1 open of 3 tasks/)).toBeTruthy()
    expect(screen.queryByText('Elsewhere')).toBeNull()
  })

  it('renders nothing when the workspace has none', async () => {
    listWorkspaceProjects.mockResolvedValue([])
    const { container } = render(<MemoryRouter><WorkspaceProjectsSection workspace="dimagi" /></MemoryRouter>)
    await Promise.resolve()
    expect(container.textContent).toBe('')
  })
})

describe('WorkspaceProjectRedirect', () => {
  const at = (path: string) =>
    render(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/w/:workspace/projects/:handle" element={<WorkspaceProjectRedirect />} />
          <Route path="/w/:workspace/agents/:agent/projects/:ref" element={<p>steward page</p>} />
        </Routes>
      </MemoryRouter>,
    )

  it("opens the project on its steward's board", async () => {
    getWorkspaceProject.mockResolvedValue(kb)
    at('/w/dimagi/projects/givewell-kb')
    expect(await screen.findByText('steward page')).toBeTruthy()
    expect(getWorkspaceProject).toHaveBeenCalledWith('dimagi', 'givewell-kb')
  })

  it('says so when there is no such project', async () => {
    getWorkspaceProject.mockRejectedValue(new AgentApiError('not found', 404, 'not found'))
    at('/w/dimagi/projects/nope')
    expect(await screen.findByText('No workspace project dimagi/nope.')).toBeTruthy()
  })
})

describe('WorkspaceProjectLine', () => {
  it('shows only for a workspace project', () => {
    const { rerender } = render(<WorkspaceProjectLine project={kb} />)
    expect(screen.getByTestId('workspace-project-line').textContent).toContain('dimagi/givewell-kb')
    rerender(<WorkspaceProjectLine project={{ ...(kb as object), scope: 'agent' } as never} />)
    expect(screen.queryByTestId('workspace-project-line')).toBeNull()
  })
})
