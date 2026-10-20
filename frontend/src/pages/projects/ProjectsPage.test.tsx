// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const listFleetProjects = vi.fn()
const listWorkspaceProjects = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  listFleetProjects: (...a: unknown[]) => listFleetProjects(...a),
  listWorkspaceProjects: (...a: unknown[]) => listWorkspaceProjects(...a),
}))

const { ProjectsPage } = await import('./ProjectsPage')

afterEach(() => {
  cleanup()
  listFleetProjects.mockReset()
  listWorkspaceProjects.mockReset()
})

const project = (over: object) => ({
  id: 1, agent_slug: 'eva', workspace: 'dimagi', scope: 'agent', handle: '', steward_slug: null, address: '',
  url: '/w/dimagi/agents/eva/projects/P1', board_url: '/w/dimagi/agents/eva/projects/P1', ext_id: 'P1',
  name: 'IDM talk', outcome: 'A talk', status: 'active', owner_email: 'jj@dimagi.com',
  task_count: 3, open_task_count: 2, waiting_task_count: 1, updated_at: new Date().toISOString(),
  ...over,
})

const show = () =>
  render(
    <MemoryRouter initialEntries={['/w/dimagi/projects']}>
      <Routes><Route path="/w/:workspace/projects" element={<ProjectsPage />} /></Routes>
    </MemoryRouter>,
  )

describe('ProjectsPage', () => {
  it("lists the agents' projects and the workspace's own, once each", async () => {
    const kb = project({ id: 2, scope: 'workspace', handle: 'kb', steward_slug: 'hal', address: 'dimagi/kb', name: 'GiveWell KB' })
    listFleetProjects.mockResolvedValue([project({}), kb])
    listWorkspaceProjects.mockResolvedValue([kb, project({ id: 3, workspace: 'connect', name: 'Elsewhere' })])
    show()
    expect(await screen.findByText('IDM talk')).toBeTruthy()
    expect(screen.getAllByText('GiveWell KB')).toHaveLength(1)
    expect(screen.getByText('workspace · dimagi/kb')).toBeTruthy()
    expect(screen.queryByText('Elsewhere')).toBeNull()
  })

  it('hides done and archived until asked', async () => {
    listFleetProjects.mockResolvedValue([project({}), project({ id: 4, name: 'Shipped', status: 'done' })])
    listWorkspaceProjects.mockResolvedValue([])
    show()
    expect(await screen.findByText('IDM talk')).toBeTruthy()
    expect(screen.queryByText('Shipped')).toBeNull()
    fireEvent.click(screen.getByText(/1 done or archived/))
    expect(screen.getByText('Shipped')).toBeTruthy()
  })
})
