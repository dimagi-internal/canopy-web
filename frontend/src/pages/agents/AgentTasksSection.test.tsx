// @vitest-environment jsdom
//
// An agent's Tasks tab is the workspace task list pinned to the agent. Old
// links keep working: `?waiting=me` (and the retired Inbox, which redirects
// to it) opens Waiting on me; `?batch=` opens one sitting, settled tasks too.
// A change made here re-reads the rail's waiting badge.
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

const { listTasks, updateTask, refreshWaiting } = vi.hoisted(() => ({
  listTasks: vi.fn<(q?: unknown, ws?: string) => Promise<unknown[]>>(async () => []),
  updateTask: vi.fn(async (key: string, body: Record<string, unknown>) => ({ ...task({ key }), ...body })),
  refreshWaiting: vi.fn(),
}))

vi.mock('@/api/work', async (orig) => ({
  ...(await orig<typeof import('@/api/work')>()),
  listTasks,
  updateTask,
  countTasks: vi.fn(async () => 2),
  getTaskSettings: vi.fn(async () => ({ workspace: 'connect', prefix: 'CON', status_names: {}, update_cadence_days: 7 })),
  listViews: vi.fn(async () => []),
}))
vi.mock('@/components/work/useDirectory', () => ({
  useDirectory: () => ({ people: [], agents: [], labels: [], projects: [] }),
  invalidateDirectory: () => {},
}))
vi.mock('@/components/work/TaskDetailView', () => ({ TaskDetailView: () => null }))
vi.mock('@/widget/usePageState', () => ({ usePageState: () => {} }))
vi.mock('@/workspace/WorkspaceProvider', () => ({ useWorkspaceList: () => [{ slug: 'connect', role: 'owner' }] }))
// The router test imports the whole route table; the fleet page is not under test here.
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
const { KeepQueryRedirect } = await import('./KeepQueryRedirect')

function task(over: Record<string, unknown> = {}) {
  return {
    id: 1, key: 'CON-1', url: '', workspace: 'connect', ext_id: 'T1', kind: 'work', title: 'Ship the thing',
    next_action: '', status: 'in_progress', category: 'started', priority: 0, labels: [], owner: '',
    assigned: '', agent_slug: 'eva', assignee_email: null, ask_kind: '', ask_body: '', ask_open: false,
    batch_key: '', idempotency_key: '', origin: '', confidence: '', score: '', review: '', rationale: '',
    source_url: '', plan: '', due: null, notes: '', position: 0, rank: 0, drive_folder_id: '',
    drive_folder_url: '', created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z',
    ...over,
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
        <Route path="/w/:workspace/agents/:slug/tasks" element={<><AgentTasksSection /><Where /></>} />
      </Routes>
    </MemoryRouter>,
  )
}

const lastCall = () => listTasks.mock.calls.at(-1) as [Record<string, unknown>, string]

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentTasksSection', () => {
  it('is the workspace list pinned to the agent, open work, All first', async () => {
    show('/w/connect/agents/eva/tasks')
    await waitFor(() => expect(lastCall()).toEqual([{ agent: 'eva', category: 'triage,backlog,unstarted,started' }, 'connect']))
    expect(screen.getByTestId('tab-all').getAttribute('aria-selected')).toBe('true')
    expect(screen.queryByTestId('tab-mine')).toBeNull()
  })

  it('reads ?waiting=me as the Waiting on me tab', async () => {
    show('/w/connect/agents/eva/tasks?waiting=me')
    await waitFor(() => expect(lastCall()[0]).toMatchObject({ agent: 'eva', waiting: 'me' }))
    expect(screen.getByTestId('tab-waiting').getAttribute('aria-selected')).toBe('true')
    // A filter change from an old link keeps what the link asked for, in the new words.
    fireEvent.change(screen.getByLabelText('Priority'), { target: { value: '1' } })
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/w/connect/agents/eva/tasks?tab=waiting&priority=1'))
  })

  it('opens one sitting on ?batch=, settled tasks included', async () => {
    show('/w/connect/agents/eva/tasks?batch=fleet-audit-2026-07-14')
    await waitFor(() => expect(lastCall()[0]).toEqual({ agent: 'eva', batch: 'fleet-audit-2026-07-14' }))
    fireEvent.click(screen.getByTestId('filter-batch'))
    await waitFor(() => expect(lastCall()[0]).toEqual({ agent: 'eva', category: 'triage,backlog,unstarted,started' }))
  })

  it('an edit re-reads the waiting badge', async () => {
    listTasks.mockResolvedValue([task()])
    show('/w/connect/agents/eva/tasks')
    fireEvent.change(await screen.findByLabelText('Due date'), { target: { value: '2026-11-01' } })
    await waitFor(() => expect(updateTask).toHaveBeenCalledWith('CON-1', { due: '2026-11-01' }))
    await waitFor(() => expect(refreshWaiting).toHaveBeenCalled())
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
      if (el?.type === Navigate || el?.type === KeepQueryRedirect) return c
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
    // Old links keep their query: a batch permalink still opens its sitting.
    ['/w/connect/agents/eva/items?batch=fa-1', '/w/connect/agents/eva/tasks?batch=fa-1'],
    ['/w/connect/agents/eva/work?batch=fa-1', '/w/connect/agents/eva/tasks?batch=fa-1'],
    ['/w/connect/agents/eva/inbox?batch=fa-1', '/w/connect/agents/eva/tasks?batch=fa-1&waiting=me'],
  ])('%s lands on %s', async (from, to) => {
    expect(await land(from)).toBe(to)
  })
})
