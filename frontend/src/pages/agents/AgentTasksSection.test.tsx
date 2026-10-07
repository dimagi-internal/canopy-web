// @vitest-environment jsdom
//
// Tasks is the board plus one filter bar. The filters are the URL — a link to
// "what is waiting on me" is `?waiting=me`, and the old Inbox/Work addresses
// redirect onto these same views rather than onto a page that no longer exists.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import {
  createMemoryRouter,
  MemoryRouter,
  Navigate,
  Outlet,
  Route,
  RouterProvider,
  Routes,
  useLocation,
  type RouteObject,
} from 'react-router-dom'

const { listTasks, listTaskActions, listProjects, actOnTask, refreshWaiting } = vi.hoisted(() => ({
  listTasks: vi.fn<(slug: string, f?: unknown) => Promise<unknown[]>>(async () => []),
  listTaskActions: vi.fn<() => Promise<unknown[]>>(async () => []),
  listProjects: vi.fn<() => Promise<unknown[]>>(async () => []),
  actOnTask: vi.fn(async () => ({})),
  refreshWaiting: vi.fn(),
}))

vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  listTasks, listTaskActions, listProjects, actOnTask,
}))
vi.mock('@/pages/agents/QuickTurn', () => ({ QuickTurn: () => <div>quick-turn</div> }))
vi.mock('@/widget/usePageState', () => ({ usePageState: () => {} }))
// The router test imports the whole route table; the fleet page is rebuilt in
// its own task and is not under test here.
vi.mock('@/pages/SupervisorPage', () => ({ default: () => null }))
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useOutletContext: () => ({
    agent: { slug: 'eva', name: 'Eva', workspace: 'connect' },
    canEdit: true,
    waiting: 3,
    refreshWaiting,
  }),
}))

const { AgentTasksSection } = await import('./AgentTasksSection')

function task(over: Record<string, unknown> = {}) {
  return {
    agent_slug: 'eva', ext_id: 'T1', title: 'Ship the thing', next_action: '',
    status: 'in_progress', owner: '', assigned: 'eva', confidence: '', score: '',
    review: '', rationale: '', source_url: '', plan: '', notes: '', position: 1,
    created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z',
    ask_kind: '', ask_body: '', ask_open: false, links: [],
    project_ext_id: null, project_name: null, ...over,
  }
}

function project(over: Record<string, unknown> = {}) {
  return {
    id: 2, agent_slug: 'eva', ext_id: 'P2', name: 'Connect Enterprise', outcome: '',
    status: 'active', owner_note: '', drive_folder_id: '', drive_folder_url: '',
    repo_slug: '', notes: '', links: [], task_count: 1, open_task_count: 1, ...over,
  }
}

function Where() {
  const loc = useLocation()
  return <div data-testid="where">{loc.pathname + loc.search + loc.hash}</div>
}

function show(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route
          path="/w/:workspace/agents/:slug/tasks"
          element={<><AgentTasksSection /><Where /></>}
        />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('AgentTasksSection', () => {
  it('reads ?waiting=me and asks the server for exactly that', async () => {
    show('/w/connect/agents/eva/tasks?waiting=me')
    await waitFor(() => expect(listTasks).toHaveBeenCalledWith('eva', { waiting: 'me' }))
    const chip = screen.getByTestId('filter-waiting')
    expect(chip.getAttribute('aria-pressed')).toBe('true')
    expect(chip.textContent).toContain('Waiting on you · 3')
  })

  it('defaults to Open: suggested and in progress', async () => {
    show('/w/connect/agents/eva/tasks')
    await waitFor(() =>
      expect(listTasks).toHaveBeenCalledWith('eva', { status: 'suggested,in_progress' }))
    expect(screen.getByTestId('filter-open').getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByTestId('filter-waiting').getAttribute('aria-pressed')).toBe('false')
  })

  it('Done rewrites the URL and refetches the finished tasks', async () => {
    show('/w/connect/agents/eva/tasks?waiting=me')
    await waitFor(() => expect(listTasks).toHaveBeenCalledWith('eva', { waiting: 'me' }))
    fireEvent.click(screen.getByTestId('filter-done'))
    await waitFor(() =>
      expect(listTasks).toHaveBeenCalledWith('eva', { status: 'done,declined' }))
    expect(screen.getByTestId('where').textContent).toBe('/w/connect/agents/eva/tasks?view=done')
    expect(screen.getByTestId('filter-done').getAttribute('aria-pressed')).toBe('true')
  })

  it('narrows to one project, or to tasks with none', async () => {
    listProjects.mockResolvedValue([project()])
    show('/w/connect/agents/eva/tasks')
    await waitFor(() => expect(screen.getByRole('option', { name: /Connect Enterprise/ })).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Project'), { target: { value: 'P2' } })
    await waitFor(() => expect(listTasks).toHaveBeenCalledWith(
      'eva', { status: 'suggested,in_progress', project: 'P2' }))
    expect(screen.getByTestId('where').textContent).toBe('/w/connect/agents/eva/tasks?project=P2')

    fireEvent.change(screen.getByLabelText('Project'), { target: { value: 'none' } })
    await waitFor(() => expect(listTasks).toHaveBeenCalledWith(
      'eva', { status: 'suggested,in_progress', project: 'none' }))
  })

  it('groups by project on ?by=project', async () => {
    listProjects.mockResolvedValue([project()])
    listTasks.mockResolvedValue([
      task({ ext_id: 'T1', project_ext_id: 'P2', project_name: 'Connect Enterprise' }),
      task({ ext_id: 'T2' }),
    ])
    show('/w/connect/agents/eva/tasks')
    await waitFor(() => expect(screen.getByTestId('task-T1')).toBeTruthy())
    fireEvent.click(screen.getByLabelText('Group by project'))
    await waitFor(() => expect(screen.getByTestId('project-P2')).toBeTruthy())
    expect(screen.getByTestId('project-none')).toBeTruthy()
    expect(screen.getByTestId('where').textContent).toBe('/w/connect/agents/eva/tasks?by=project')
  })

  it('an action on a card refetches the list and the waiting count', async () => {
    listTasks.mockResolvedValue([task({ ext_id: 'T1' })])
    show('/w/connect/agents/eva/tasks')
    await waitFor(() => expect(screen.getByTestId('task-T1')).toBeTruthy())
    const before = listTasks.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Mark done' }))
    await waitFor(() => expect(actOnTask).toHaveBeenCalled())
    expect((actOnTask.mock.calls[0] as unknown[]).slice(0, 3)).toEqual(['eva', 'T1', 'done'])
    await waitFor(() => expect(listTasks.mock.calls.length).toBeGreaterThan(before))
    expect(refreshWaiting).toHaveBeenCalled()
  })
})

describe('old agent addresses', () => {
  // The REAL route objects, so a typo in a redirect target fails here. Every
  // redirect is kept; every real page is replaced by a probe that prints where
  // the router landed.
  async function land(path: string): Promise<string> {
    const { routeTable } = await import('@/router')
    const find = (rs: RouteObject[]): RouteObject | undefined => {
      for (const r of rs) {
        if (r.path === '/w/:workspace/agents/:slug') return r
        const hit = r.children && find(r.children)
        if (hit) return hit
      }
    }
    const agentRoute = find(routeTable)
    if (!agentRoute?.children) throw new Error('agent route not found')
    const children = agentRoute.children.map((c): RouteObject => {
      const el = c.element as { type?: unknown } | undefined
      if (el?.type === Navigate) return c
      return c.index ? { index: true, element: <Where /> } : { path: c.path, element: <Where /> }
    })
    const router = createMemoryRouter(
      [{ path: '/w/:workspace/agents/:slug', element: <Outlet />, children }],
      { initialEntries: [path] },
    )
    render(<RouterProvider router={router} />)
    return (await screen.findByTestId('where')).textContent ?? ''
  }

  it('opens on Projects', async () => {
    const { routeTable } = await import('@/router')
    const flat = (rs: RouteObject[]): RouteObject[] => rs.flatMap((r) => [r, ...flat(r.children ?? [])])
    const agentRoute = flat(routeTable).find((r) => r.path === '/w/:workspace/agents/:slug')
    const index = agentRoute?.children?.find((c) => c.index)
    const el = index?.element as { type?: unknown; props?: { to?: string } } | undefined
    expect(el?.type).toBe(Navigate)
    expect(el?.props?.to).toBe('projects')
  })

  it.each([
    ['/w/connect/agents/eva/inbox', '/w/connect/agents/eva/tasks?waiting=me'],
    ['/w/connect/agents/eva/needs-you', '/w/connect/agents/eva/tasks?waiting=me'],
    ['/w/connect/agents/eva/work', '/w/connect/agents/eva/tasks'],
    ['/w/connect/agents/eva/items', '/w/connect/agents/eva/tasks'],
    ['/w/connect/agents/eva/syncs', '/w/connect/agents/eva/turns#status-reports'],
    ['/w/connect/agents/eva/tasks', '/w/connect/agents/eva/tasks'],
  ])('%s lands on %s', async (from, to) => {
    expect(await land(from)).toBe(to)
  })
})
