// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const getWorkspaceProject = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getWorkspaceProject: (...a: unknown[]) => getWorkspaceProject(...a),
}))

const { ProjectMcpInstall, WorkspaceProjectLine, WorkspaceProjectRedirect } = await import('./WorkspaceProjects')
const { AgentApiError } = await import('@/api/agents')

afterEach(() => {
  cleanup()
  getWorkspaceProject.mockReset()
})

const kb = {
  id: 1, agent_slug: 'eva', workspace: 'dimagi', scope: 'workspace', handle: 'givewell-kb', steward_slug: 'eva',
  address: 'dimagi/givewell-kb', url: '/w/dimagi/projects/givewell-kb',
  board_url: '/w/dimagi/agents/eva/projects/P12', ext_id: 'P12', name: 'GiveWell Knowledge Base',
  outcome: 'Everything GiveWell publishes, cited', status: 'active', task_count: 3, open_task_count: 1,
} as never

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

  it("goes to the steward's own workspace when it keeps a division's project", async () => {
    getWorkspaceProject.mockResolvedValue({
      ...(kb as object), workspace: 'connect', address: 'connect/givewell-kb',
    } as never)
    render(
      <MemoryRouter initialEntries={['/w/connect/projects/givewell-kb']}>
        <Routes>
          <Route path="/w/:workspace/projects/:handle" element={<WorkspaceProjectRedirect />} />
          <Route path="/w/dimagi/agents/:agent/projects/:ref" element={<p>dimagi board</p>} />
        </Routes>
      </MemoryRouter>,
    )
    expect(await screen.findByText('dimagi board')).toBeTruthy()
  })

  it("renders the project in place for a reader who cannot open the steward's board", async () => {
    getWorkspaceProject.mockResolvedValue({
      ...(kb as object), workspace: 'connect', address: 'connect/givewell-kb', board_readable: false,
      tasks: [{ ext_id: 'T1', title: 'Pull the SMC parameters', agent_slug: 'ace', status: 'todo' }],
    } as never)
    at('/w/connect/projects/givewell-kb')
    expect(await screen.findByText('GiveWell Knowledge Base')).toBeTruthy()
    expect(screen.getByText('Pull the SMC parameters')).toBeTruthy()
    expect(screen.queryByText('steward page')).toBeNull()
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

describe('ProjectMcpInstall', () => {
  it('gives the one line to add its server, named by its handle', () => {
    render(<ProjectMcpInstall project={{ ...(kb as object), mcp_path: '/api/mcp/projects/connect/givewell-kb/' } as never} />)
    expect(screen.getByTestId('project-mcp-claude-code').textContent).toBe(
      `claude mcp add --transport http givewell-kb ${window.location.origin}/api/mcp/projects/connect/givewell-kb`,
    )
  })

  it('shows nothing for a project that serves nothing', () => {
    const { container } = render(<ProjectMcpInstall project={kb} />)
    expect(container.textContent).toBe('')
  })
})
