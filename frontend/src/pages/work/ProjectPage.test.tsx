// @vitest-environment jsdom
//
// A project's one page: its sections in one order for everyone, a teammate sees
// the tasks and the progress without the controls, an editor posts an update,
// publishes an agent's draft, and adds a milestone; a milestone filters the tasks.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const api = vi.hoisted(() => ({
  getProjectByAddress: vi.fn(),
  listTasks: vi.fn(async () => [] as unknown[]),
  updateProject: vi.fn(async () => ({})),
  postUpdate: vi.fn(async () => ({})),
  publishUpdate: vi.fn(async () => ({})),
  createMilestone: vi.fn(async () => ({})),
  updateMilestone: vi.fn(async () => ({})),
  deleteMilestone: vi.fn(async () => undefined),
  createTask: vi.fn(async () => ({})),
}))
vi.mock('@/api/work', async (orig) => ({ ...(await orig<typeof import('@/api/work')>()), ...api }))
vi.mock('@/components/work/useDirectory', () => ({
  useDirectory: () => ({ people: [], agents: [], labels: [], projects: [] }),
  invalidateDirectory: () => {},
}))
vi.mock('@/components/projects/useEngagementView', () => ({ useEngagementView: () => {} }))
vi.mock('@/components/projects/ProposalCard', () => ({
  ProposalCard: ({ proposal }: { proposal?: { name?: string } }) => <div data-testid="proposal-card">{proposal?.name ?? 'task'}</div>,
}))
vi.mock('@/pages/agents/ProjectOverview', () => ({
  ProjectOverview: ({ agent, projectRef }: { agent: string; projectRef: string }) => (
    <section id="team-ideas" data-testid="project-overview">
      ideas of {agent}/{projectRef}
    </section>
  ),
}))
vi.mock('@/pages/agents/ProjectCheckins', () => ({
  CheckinsPanel: () => <div data-testid="checkins-panel" />,
  CheckinTimeline: () => <div data-testid="checkin-timeline" />,
}))

const { ProjectPage } = await import('./ProjectPage')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function task(over: Record<string, unknown> = {}) {
  return {
    key: 'DIM-1', url: '/w/dimagi/tasks/DIM-1', agent_slug: 'eva', assignee_email: null, workspace: 'dimagi',
    ext_id: 'T1', project_address: 'dimagi/idm-talk', project_handle: 'idm-talk', project_name: 'IDM talk',
    parent_key: null, kind: 'work', title: 'Draft the slides', status: 'in_progress', category: 'started',
    priority: 0, labels: [], milestone: null, ask_open: false, ask_kind: '', assigned: '', updated_at: new Date().toISOString(),
    ...over,
  }
}

function detail(over: Record<string, unknown> = {}) {
  return {
    id: 7, agent_slug: 'eva', steward_slug: 'eva', member_agents: ['eva', 'hal'], workspace: 'dimagi', handle: 'idm-talk',
    address: 'dimagi/idm-talk', url: '/w/dimagi/projects/idm-talk', board_url: '', mcp_kind: '', mcp_path: '',
    ext_id: 'P3', name: 'IDM talk', outcome: 'A talk people remember', status: 'active', priority: 2,
    start_date: '2026-10-01', target_date: '2026-11-15', owner_email: 'jj@dimagi.com', health: 'at_risk',
    update_missing: false, done_task_count: 1, drive_folder_url: 'https://drive.example/f', repo_slug: '', links: [],
    task_count: 3, open_task_count: 2, waiting_task_count: 0, created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-20T00:00:00Z',
    progress: {
      total: 3, done: 1, started: 1,
      scope_history: [
        { at: '2026-10-06', scope: 2, done: 0 },
        { at: '2026-10-13', scope: 3, done: 1 },
      ],
    },
    milestones: [{ id: 11, name: 'Outline', description: '', target_date: '2026-10-30', rank: 0, done: 1, total: 2 }],
    latest_update: {
      id: 21, health: 'at_risk', body: 'Slides are **late**.', status: 'published', author_email: 'jj@dimagi.com',
      author_agent: null, published_by_email: 'jj@dimagi.com', diff: { tasks_done: 2, tasks_added: 1, target_date: ['2026-11-01', '2026-11-15'] },
      published_at: new Date().toISOString(), created_at: new Date().toISOString(),
    },
    draft_updates: [
      { id: 22, health: 'on_track', body: 'Eva thinks it is fine.', status: 'draft', author_agent: 'eva', author_email: null, diff: {}, created_at: new Date().toISOString() },
    ],
    may_edit: true, tasks: [], participants: [], proposal: null, proposed_tasks: [], recent_turns: [
      { id: 'turn-1', status: 'done', prompt_preview: 'Work on the slides', created_at: new Date().toISOString() },
    ],
    board_readable: true,
    ...over,
  }
}

function show() {
  return render(
    <MemoryRouter initialEntries={['/w/dimagi/projects/idm-talk']}>
      <Routes>
        <Route path="/w/:workspace/projects/:handle" element={<ProjectPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ProjectPage', () => {
  it('shows its sections in one order', async () => {
    api.getProjectByAddress.mockResolvedValue(detail({ proposal: { name: 'IDM talk' } }))
    api.listTasks.mockResolvedValue([task()])
    show()
    await screen.findByTestId('project-header')
    expect(api.getProjectByAddress).toHaveBeenCalledWith('dimagi', 'idm-talk')
    const ids = Array.from(document.querySelectorAll('[data-testid="project-page"] section[id]')).map((s) => s.id)
    expect(ids).toEqual([
      'project-update',
      'project-milestones',
      'project-scope',
      'project-tasks',
      'project-services',
      'project-proposal',
      'team-ideas',
      'project-checkins',
      'project-timeline',
      'project-activity',
    ])
    // The ideas and check-ins are read through the lead agent's alias for it.
    expect(screen.getByTestId('project-overview').textContent).toContain('eva/P3')
    expect(screen.getByText('← Projects').getAttribute('href')).toBe('/w/dimagi/projects')
  })

  it('reads its tasks by its address, open ones unless asked', async () => {
    api.getProjectByAddress.mockResolvedValue(detail())
    api.listTasks.mockResolvedValue([task()])
    show()
    expect(await screen.findByText('Draft the slides')).toBeTruthy()
    expect(api.listTasks).toHaveBeenCalledWith(
      { project: 'dimagi/idm-talk', category: 'triage,backlog,unstarted,started', limit: 500 },
      'dimagi',
    )
    fireEvent.click(screen.getByLabelText('Show done and canceled'))
    await waitFor(() => expect(api.listTasks).toHaveBeenLastCalledWith({ project: 'dimagi/idm-talk', limit: 500 }, 'dimagi'))
  })

  it('shows a teammate the tasks and progress, without the controls', async () => {
    api.getProjectByAddress.mockResolvedValue(detail({ may_edit: false }))
    api.listTasks.mockResolvedValue([task()])
    show()
    expect(await screen.findByText('Draft the slides')).toBeTruthy()
    expect(screen.getByTestId('project-progress').textContent).toContain('1 of 3 done')
    expect(screen.getByTestId('scope-chart')).toBeTruthy()
    expect(screen.getByText('IDM talk', { selector: 'h1' })).toBeTruthy()
    expect(screen.getByTestId('latest-update').textContent).toContain('Slides are late.')
    expect(screen.getByTestId('update-diff').textContent).toContain('2 tasks done · 1 added · target 2026-11-01 → 2026-11-15')
    // The draft is there to read, with nothing to press.
    expect(screen.getByTestId('draft-22').textContent).toContain('Eva thinks it is fine.')
    expect(screen.queryByRole('button', { name: 'Publish' })).toBeNull()
    expect(screen.queryByTestId('post-update')).toBeNull()
    expect(screen.queryByLabelText('Project name')).toBeNull()
    expect(screen.queryByLabelText('Project status')).toBeNull()
    expect(screen.queryByLabelText('New task')).toBeNull()
    expect(screen.queryByLabelText('New milestone')).toBeNull()
    expect(screen.queryByLabelText('Delete Outline')).toBeNull()
  })

  it('says when an update is missing', async () => {
    api.getProjectByAddress.mockResolvedValue(detail({ update_missing: true, latest_update: null, health: null }))
    show()
    expect((await screen.findByTestId('update-missing')).textContent).toContain('Update missing')
    expect(within(screen.getByTestId('project-properties')).getByText('Update missing')).toBeTruthy()
  })

  it('posts an update with its health', async () => {
    api.getProjectByAddress.mockResolvedValue(detail())
    show()
    const form = await screen.findByTestId('post-update')
    fireEvent.click(within(form).getByLabelText('Off track'))
    fireEvent.change(within(form).getByLabelText('Update'), { target: { value: 'Speaker dropped out' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Post update' }))
    await waitFor(() =>
      expect(api.postUpdate).toHaveBeenCalledWith('dimagi', 'idm-talk', { health: 'off_track', body: 'Speaker dropped out' }),
    )
    await waitFor(() => expect(api.getProjectByAddress).toHaveBeenCalledTimes(2))
  })

  it("publishes an agent's draft with the health the publisher picks", async () => {
    api.getProjectByAddress.mockResolvedValue(detail())
    show()
    const draft = await screen.findByTestId('draft-22')
    fireEvent.click(within(draft).getByLabelText('At risk'))
    fireEvent.click(within(draft).getByRole('button', { name: 'Publish' }))
    await waitFor(() => expect(api.publishUpdate).toHaveBeenCalledWith('dimagi', 'idm-talk', 22, { health: 'at_risk' }))
  })

  it('adds a milestone, and a milestone filters the tasks', async () => {
    api.getProjectByAddress.mockResolvedValue(detail())
    api.listTasks.mockResolvedValue([task(), task({ key: 'DIM-2', title: 'Book the room', milestone: 'Outline' })])
    show()
    expect(await screen.findByText('Draft the slides')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('New milestone'), { target: { value: 'Rehearsal' } })
    fireEvent.change(screen.getByLabelText('New milestone target date'), { target: { value: '2026-11-10' } })
    fireEvent.click(within(screen.getByTestId('project-milestones-form')).getByRole('button', { name: 'Add' }))
    await waitFor(() =>
      expect(api.createMilestone).toHaveBeenCalledWith('dimagi', 'idm-talk', { name: 'Rehearsal', target_date: '2026-11-10' }),
    )

    fireEvent.click(screen.getByRole('button', { name: 'Outline' }))
    expect(screen.getByTestId('milestone-filter').textContent).toContain('Outline')
    expect(screen.queryByText('Draft the slides')).toBeNull()
    expect(screen.getByText('Book the room')).toBeTruthy()
  })

  it('changes a property through the project route', async () => {
    api.getProjectByAddress.mockResolvedValue(detail())
    show()
    fireEvent.change(await screen.findByLabelText('Project status'), { target: { value: 'paused' } })
    await waitFor(() => expect(api.updateProject).toHaveBeenCalledWith('dimagi', 'idm-talk', { status: 'paused' }))
  })

  it('starts a proposed project only from its decision card', async () => {
    api.getProjectByAddress.mockResolvedValue(detail({ status: 'proposed', proposal: { name: 'IDM talk' } }))
    show()
    await screen.findByTestId('project-header')
    expect(screen.queryByLabelText('Project status')).toBeNull()
    expect(screen.getByText('Decide below').getAttribute('href')).toBe('#project-proposal')
    expect(screen.getByTestId('proposal-card')).toBeTruthy()
  })

  it('leaves out ideas and check-ins when it has no lead agent', async () => {
    api.getProjectByAddress.mockResolvedValue(detail({ agent_slug: null, steward_slug: null, ext_id: '' }))
    show()
    await screen.findByTestId('project-header')
    expect(screen.queryByTestId('project-overview')).toBeNull()
    expect(screen.queryByTestId('checkins-panel')).toBeNull()
  })

  it('adds a task to the project', async () => {
    api.getProjectByAddress.mockResolvedValue(detail())
    show()
    fireEvent.change(await screen.findByLabelText('New task'), { target: { value: 'Rehearse' } })
    fireEvent.submit(screen.getByLabelText('New task').closest('form')!)
    await waitFor(() =>
      expect(api.createTask).toHaveBeenCalledWith(
        { title: 'Rehearse', project: 'dimagi/idm-talk', workspace: 'dimagi', status: 'todo' },
        'dimagi',
      ),
    )
  })

  it('says so when there is no such project', async () => {
    const { ApiError } = await import('@/api/problem')
    api.getProjectByAddress.mockRejectedValue(new ApiError(404, 'not found', { detail: 'not found' }))
    show()
    expect(await screen.findByText('No project dimagi/idm-talk.')).toBeTruthy()
  })
})
