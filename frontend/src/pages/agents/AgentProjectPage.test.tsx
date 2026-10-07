// @vitest-environment jsdom
//
// A project page is always the same four sections in the same order — Header,
// Tasks, Activity, Links — so a person learns one page, not one per project.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const { getProject, patchProject, ctx } = vi.hoisted(() => ({
  getProject: vi.fn<(slug: string, ref: string) => Promise<unknown>>(),
  patchProject: vi.fn(async () => ({})),
  ctx: { canEdit: true, waiting: 0, refreshWaiting: () => {} },
}))

vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getProject,
  patchProject,
}))
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useOutletContext: () => ({ agent: { slug: 'eva', name: 'Eva' }, ...ctx }),
}))

const { AgentProjectPage } = await import('./AgentProjectPage')

function task(over: Record<string, unknown> = {}) {
  return {
    agent_slug: 'eva', ext_id: 'T1', title: 'Ship the thing', next_action: '',
    status: 'in_progress', owner: '', assigned: 'eva', confidence: '', score: '',
    review: '', rationale: '', source_url: '', plan: '', notes: '', position: 1,
    batch_key: '', origin: '', created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z', ask_kind: '', ask_body: '', ask_open: false,
    links: [], project_ext_id: 'P1', project_name: 'Connect Enterprise', ...over,
  }
}

function detail(over: Record<string, unknown> = {}) {
  return {
    id: 1, agent_slug: 'eva', ext_id: 'P1', name: 'Connect Enterprise',
    outcome: 'A clear "what" explanation of Connect Enterprise',
    status: 'active', owner_note: '', owner_email: 'jonathan@dimagi.com',
    drive_folder_id: 'abc', drive_folder_url: 'https://drive.google.com/drive/folders/abc',
    repo_slug: 'dimagi/connect', notes: '',
    links: [{ label: 'Overview doc', url: 'https://docs.google.com/document/d/x' }],
    task_count: 2, open_task_count: 1, waiting_task_count: 0,
    created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-05T00:00:00Z',
    tasks: [
      task({ ext_id: 'T9', title: 'Finished one', status: 'done' }),
      task({ ext_id: 'T1', title: 'Live one' }),
    ],
    recent_turns: [
      { id: '0b5c-uuid', status: 'succeeded', prompt_preview: 'Draft the overview',
        created_at: '2026-10-04T00:00:00Z', task_ext_ids: ['T1'] },
    ],
    ...over,
  }
}

function show() {
  return render(
    <MemoryRouter initialEntries={['/w/connect/agents/eva/projects/P1']}>
      <Routes>
        <Route path="/w/:workspace/agents/:slug/projects/:ref" element={<AgentProjectPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  ctx.canEdit = true
})

describe('AgentProjectPage', () => {
  it('renders the header then Tasks, Activity, Links — in that order', async () => {
    getProject.mockResolvedValue(detail())
    show()
    await screen.findByRole('heading', { name: 'Connect Enterprise' })
    expect(getProject).toHaveBeenCalledWith('eva', 'P1')

    const headings = screen.getAllByRole('heading').map((h) => h.textContent)
    expect(headings[0]).toBe('Connect Enterprise')
    expect(headings.slice(1)).toEqual(['Tasks', 'Activity', 'Links'])

    expect(screen.getByRole('link', { name: '← Projects' }).getAttribute('href'))
      .toBe('/w/connect/agents/eva/projects')
    expect(screen.getByRole('link', { name: 'Drive folder' }).getAttribute('href'))
      .toBe('https://drive.google.com/drive/folders/abc')
    expect(screen.getByText(/dimagi\/connect/)).toBeTruthy()
    expect(screen.getByText(/jonathan@dimagi\.com/)).toBeTruthy()
  })

  it('lists live tasks before finished ones, and turns link to the Turns page', async () => {
    getProject.mockResolvedValue(detail())
    show()
    await screen.findByTestId('task-T1')
    const cards = screen.getAllByTestId(/^task-T/).map((el) => el.getAttribute('data-testid'))
    expect(cards).toEqual(['task-T1', 'task-T9'])

    const turn = screen.getByTestId('turn-0b5c-uuid')
    expect(turn.getAttribute('href')).toBe('/w/connect/agents/eva/turns#0b5c-uuid')
    expect(turn.textContent).toContain('Draft the overview')
    expect(turn.textContent).toContain('succeeded')

    const links = screen.getByTestId('project-links') as HTMLDetailsElement
    expect(links.open).toBe(false)
    expect(within(links).getByRole('link', { name: 'Overview doc' }).getAttribute('href'))
      .toBe('https://docs.google.com/document/d/x')
  })

  it('shows each section\'s empty state', async () => {
    getProject.mockResolvedValue(detail({ tasks: [], recent_turns: [], links: [] }))
    show()
    expect(await screen.findByText('No tasks in this project yet.')).toBeTruthy()
    expect(screen.getByText('No turns have touched this project yet.')).toBeTruthy()
  })

  it('changing the status patches the project', async () => {
    getProject.mockResolvedValue(detail())
    show()
    const select = await screen.findByLabelText('Project status')
    fireEvent.change(select, { target: { value: 'done' } })
    await waitFor(() => expect(patchProject).toHaveBeenCalledWith('eva', 'P1', { status: 'done' }))
    await waitFor(() => expect(getProject).toHaveBeenCalledTimes(2))
  })

  it('a viewer sees the status but cannot change it', async () => {
    ctx.canEdit = false
    getProject.mockResolvedValue(detail())
    show()
    const select = (await screen.findByLabelText('Project status')) as HTMLSelectElement
    expect(select.disabled).toBe(true)
  })

  it('/w/x/agents/eva/projects/P1 routes to the project page', async () => {
    const { createMemoryRouter, matchRoutes, Outlet, RouterProvider } = await import('react-router-dom')
    const { routeTable } = await import('@/router')
    const matches = matchRoutes(routeTable, '/w/x/agents/eva/projects/P1') ?? []
    expect(matches.at(-1)?.route.path).toBe('projects/:ref')
    expect(matches.at(-1)?.params).toMatchObject({ workspace: 'x', slug: 'eva', ref: 'P1' })

    // Render the agent route's REAL children (lazy + LazySection) under a bare
    // outlet — the shell itself needs auth/workspace providers this test omits.
    const agentRoute = matches.at(-2)?.route
    getProject.mockResolvedValue(detail())
    const router = createMemoryRouter(
      [{ path: '/w/:workspace/agents/:slug', element: <Outlet />, children: agentRoute?.children }],
      { initialEntries: ['/w/x/agents/eva/projects/P1'] },
    )
    render(<RouterProvider router={router} />)
    expect(await screen.findByRole('heading', { name: 'Connect Enterprise' })).toBeTruthy()
    expect(getProject).toHaveBeenCalledWith('eva', 'P1')
  })
})
