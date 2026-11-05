// @vitest-environment jsdom
//
// The workspace's Projects table: open projects by default, each with its
// health or "Update missing", and "New project" makes one and opens its page.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const api = vi.hoisted(() => ({
  listProjects: vi.fn(async () => [] as unknown[]),
  createWorkspaceProject: vi.fn(),
}))
vi.mock('@/api/work', async (orig) => ({ ...(await orig<typeof import('@/api/work')>()), ...api }))
vi.mock('@/components/work/useDirectory', () => ({
  useDirectory: () => ({
    people: [{ user_id: 1, email: 'jj@dimagi.com' }],
    agents: [{ slug: 'eva', name: 'Eva' }],
    labels: [],
    projects: [],
  }),
  invalidateDirectory: () => {},
}))
vi.mock('@/workspace/WorkspaceProvider', async (orig) => ({
  ...(await orig<typeof import('@/workspace/WorkspaceProvider')>()),
  useWorkspaceRole: () => 'editor',
}))

const { ProjectsPage } = await import('./ProjectsPage')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const project = (over: Record<string, unknown>) => ({
  id: 1, agent_slug: 'eva', workspace: 'dimagi', handle: 'idm-talk', steward_slug: 'eva', member_agents: ['eva', 'hal'],
  address: 'dimagi/idm-talk', url: '/w/dimagi/projects/idm-talk', ext_id: 'P1', name: 'IDM talk', outcome: 'A talk',
  status: 'active', owner_email: 'jj@dimagi.com', health: 'on_track', update_missing: false, target_date: '2026-11-15',
  task_count: 4, done_task_count: 1, open_task_count: 2, waiting_task_count: 1, updated_at: new Date().toISOString(),
  ...over,
})

const show = () =>
  render(
    <MemoryRouter initialEntries={['/w/dimagi/projects']}>
      <Routes>
        <Route path="/w/:workspace/projects" element={<ProjectsPage />} />
        <Route path="/w/:workspace/projects/:handle" element={<p>project page</p>} />
      </Routes>
    </MemoryRouter>,
  )

describe('ProjectsPage', () => {
  it('lists open projects with their health, lead, agents and progress', async () => {
    api.listProjects.mockResolvedValue([
      project({}),
      project({ id: 2, handle: 'kb', url: '/w/dimagi/projects/kb', name: 'GiveWell KB', health: null, update_missing: true }),
    ])
    show()
    const link = await screen.findByRole('link', { name: 'IDM talk' })
    expect(api.listProjects).toHaveBeenCalledWith('open', 'dimagi')
    expect(link.getAttribute('href')).toBe('/w/dimagi/projects/idm-talk')
    const row = link.closest('tr')!
    expect(within(row).getByText('Active')).toBeTruthy()
    expect(within(row).getByText('On track')).toBeTruthy()
    expect(within(row).getByText('jj')).toBeTruthy()
    expect(within(row).getByText('Eva')).toBeTruthy()
    expect(within(row).getByText('Hal')).toBeTruthy()
    expect(within(row).getByText('1/4')).toBeTruthy()
    const kb = screen.getByRole('link', { name: 'GiveWell KB' }).closest('tr')!
    expect(within(kb).getByText('Update missing')).toBeTruthy()
  })

  it('asks the server for All, and filters and sorts what it has', async () => {
    api.listProjects.mockResolvedValue([
      project({ id: 1, name: 'Beta', target_date: '2026-12-01' }),
      project({ id: 2, name: 'Alpha', target_date: '2026-11-01' }),
    ])
    show()
    await screen.findByText('Beta')
    fireEvent.click(screen.getByRole('button', { name: 'All' }))
    await waitFor(() => expect(api.listProjects).toHaveBeenLastCalledWith('', 'dimagi'))
    await screen.findByText('Beta')
    fireEvent.change(screen.getByLabelText('Sort'), { target: { value: 'target' } })
    const names = within(screen.getByRole('table')).getAllByRole('link').map((a) => a.textContent)
    expect(names).toEqual(['Alpha', 'Beta'])
    fireEvent.change(screen.getByLabelText('Filter projects'), { target: { value: 'bet' } })
    expect(screen.queryByText('Alpha')).toBeNull()
  })

  it('creates a project from the dialog and opens it', async () => {
    api.createWorkspaceProject.mockResolvedValue(project({ id: 9, handle: 'launch', url: '/w/dimagi/projects/launch' }))
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'New project' }))
    const dialog = await screen.findByTestId('new-project-dialog')
    fireEvent.change(within(dialog).getByLabelText('Name'), { target: { value: 'Launch' } })
    fireEvent.change(within(dialog).getByLabelText('Outcome'), { target: { value: 'It is live' } })
    fireEvent.change(within(dialog).getByLabelText('Lead agent'), { target: { value: 'eva' } })
    fireEvent.change(within(dialog).getByLabelText('Lead'), { target: { value: 'jj@dimagi.com' } })
    fireEvent.change(within(dialog).getByLabelText('Target date'), { target: { value: '2026-12-01' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create project' }))
    await waitFor(() =>
      expect(api.createWorkspaceProject).toHaveBeenCalledWith({
        name: 'Launch',
        workspace: 'dimagi',
        outcome: 'It is live',
        steward: 'eva',
        member_agents: ['eva'],
        owner_email: 'jj@dimagi.com',
        target_date: '2026-12-01',
      }),
    )
    expect(await screen.findByText('project page')).toBeTruthy()
  })

  it('asks who can see a new project, as a choice, and says what Just me means', async () => {
    api.createWorkspaceProject.mockResolvedValue(project({ url: '/w/dimagi/projects/mine' }))
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'New project' }))
    const dialog = within(await screen.findByTestId('new-project-dialog'))
    fireEvent.change(dialog.getByLabelText('Name'), { target: { value: 'Mine' } })
    expect(dialog.getByRole('group', { name: 'Who can see this' })).toBeTruthy()
    expect((dialog.getByRole('radio', { name: 'Workspace' }) as HTMLInputElement).checked).toBe(true)
    expect(dialog.queryByTestId('personal-explainer')).toBeNull()
    fireEvent.click(dialog.getByRole('radio', { name: 'Just me' }))
    expect(dialog.getByTestId('personal-explainer').textContent).toContain('Workspace admins cannot see it')
    // The lead of a project only you see is you: nothing to pick.
    expect(dialog.queryByLabelText('Lead')).toBeNull()
    fireEvent.click(dialog.getByRole('button', { name: 'Create project' }))
    await waitFor(() => expect(api.createWorkspaceProject).toHaveBeenCalled())
    expect(api.createWorkspaceProject.mock.calls[0][0]).toMatchObject({ name: 'Mine', visibility: 'personal' })
    expect(api.createWorkspaceProject.mock.calls[0][0]).not.toHaveProperty('owner_email')
  })

  it('marks a personal project in the list, and no other', async () => {
    api.listProjects.mockResolvedValue([
      project({ id: 1, name: 'Mine', visibility: 'personal', address: 'dimagi/mine' }),
      project({ id: 2, name: 'Ours', visibility: 'workspace', address: 'dimagi/ours' }),
    ])
    show()
    await screen.findByText('Mine')
    const badges = screen.getAllByTestId('personal-badge')
    expect(badges).toHaveLength(1)
    expect(badges[0].textContent).toBe('Personal')
    expect(badges[0].closest('[data-project]')?.getAttribute('data-project')).toBe('dimagi/mine')
  })
})
