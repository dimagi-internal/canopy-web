// @vitest-environment jsdom
//
// An agent's Projects tab is the workspace's Projects table narrowed to the
// projects it leads or works on; "Add project" still creates through the agent.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const { agentProjects, workProjects, createProject, ctx } = vi.hoisted(() => ({
  agentProjects: vi.fn(async () => [] as unknown[]),
  workProjects: vi.fn(async () => [] as unknown[]),
  createProject: vi.fn(async () => ({})),
  ctx: { canEdit: true, waiting: 0, refreshWaiting: () => {} },
}))

vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  listProjects: agentProjects,
  createProject,
}))
vi.mock('@/api/work', async (orig) => ({ ...(await orig<typeof import('@/api/work')>()), listProjects: workProjects }))
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useOutletContext: () => ({ agent: { slug: 'eva', name: 'Eva' }, ...ctx }),
}))

const { AgentProjectsSection } = await import('./AgentProjectsSection')

function project(over: Record<string, unknown> = {}) {
  return {
    id: 1, agent_slug: 'eva', member_agents: ['eva'], workspace: 'connect', handle: 'enterprise', ext_id: 'P1',
    address: 'connect/enterprise', url: '/w/connect/projects/enterprise', name: 'Connect Enterprise',
    outcome: 'A clear explanation', status: 'active', owner_email: 'jonathan@dimagi.com', health: 'on_track',
    update_missing: false, task_count: 4, done_task_count: 1, open_task_count: 3, waiting_task_count: 1,
    created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-05T00:00:00Z', ...over,
  }
}

function show() {
  return render(
    <MemoryRouter initialEntries={['/w/connect/agents/eva/projects']}>
      <Routes>
        <Route path="/w/:workspace/agents/:slug/projects" element={<AgentProjectsSection />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  agentProjects.mockResolvedValue([])
  workProjects.mockResolvedValue([])
  ctx.canEdit = true
})

describe('AgentProjectsSection', () => {
  it('lists the projects it leads and the ones it works on, open first', async () => {
    agentProjects.mockResolvedValue([project(), project({ id: 2, name: 'Old launch', status: 'done' })])
    workProjects.mockResolvedValue([
      project(),
      project({ id: 3, agent_slug: 'hal', member_agents: ['hal', 'eva'], name: 'Hal leads, Eva helps', url: '/w/connect/projects/hal-eva' }),
      project({ id: 4, agent_slug: 'hal', member_agents: ['hal'], name: 'Not Eva' }),
    ])
    show()
    const link = await screen.findByRole('link', { name: 'Connect Enterprise' })
    expect(link.getAttribute('href')).toBe('/w/connect/projects/enterprise')
    expect(screen.getAllByRole('link', { name: 'Connect Enterprise' })).toHaveLength(1)
    expect(screen.getByText('Hal leads, Eva helps')).toBeTruthy()
    expect(screen.queryByText('Not Eva')).toBeNull()
    expect(screen.queryByText('Old launch')).toBeNull()
    expect(workProjects).toHaveBeenCalledWith('', 'connect')

    fireEvent.click(screen.getByRole('button', { name: 'All' }))
    expect(screen.getByText('Old launch')).toBeTruthy()
  })

  it('says when the agent has no projects', async () => {
    show()
    expect(await screen.findByText(/Eva has no projects yet/)).toBeTruthy()
  })

  it('adds a project through the agent, so it carries its P-number', async () => {
    show()
    await screen.findByText(/Eva has no projects yet/)
    fireEvent.change(screen.getByLabelText('New project name'), { target: { value: 'Launch' } })
    fireEvent.change(screen.getByLabelText('New project outcome'), { target: { value: 'It is live' } })
    fireEvent.submit(screen.getByLabelText('New project name').closest('form')!)
    await waitFor(() => expect(createProject).toHaveBeenCalledWith('eva', { name: 'Launch', outcome: 'It is live' }))
  })
})
