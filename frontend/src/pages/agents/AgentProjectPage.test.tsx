// @vitest-environment jsdom
//
// A project has ONE page, for everyone: what it is, where it stands, the ideas,
// the people (ProjectOverview) — then, for whoever runs the work, the agent's
// tasks and the turns that touched them.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const { getProject, patchProject, getProjectTeam, ctx } = vi.hoisted(() => ({
  getProject: vi.fn<(slug: string, ref: string) => Promise<unknown>>(),
  patchProject: vi.fn(async () => ({})),
  getProjectTeam: vi.fn(async () => teamView()),
  ctx: { canEdit: true, waiting: 0, refreshWaiting: () => {} },
}))

vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getProject,
  patchProject,
  getProjectTeam,
}))
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useOutletContext: () => ({ agent: { slug: 'eva', name: 'Eva' }, ...ctx }),
}))

function teamView() {
  const live = {
    ext_id: 'T1',
    title: 'Live one',
    quote: 'the normal case is someone just ignores us',
    who: { person_id: 7, name: 'Sophie' },
    source_url: 'https://canopy.test/chat/1',
    said_at: '2026-09-27T00:00:00Z',
    stage: 'started',
    status: 'in_progress',
    people: [7],
    subtasks: [{ ext_id: 'T2', title: 'Review', status: 'done' }],
    created_at: '2026-10-05T00:00:00Z',
    updated_at: '2026-10-05T00:00:00Z',
  }
  const done = {
    ext_id: 'T9',
    title: 'Finished one',
    quote: 'Finished one',
    who: { person_id: null, name: 'A Partner' },
    source_url: '',
    said_at: null,
    stage: 'shipped',
    status: 'done',
    people: [],
    subtasks: [],
    created_at: '2026-10-05T00:00:00Z',
    updated_at: '2026-10-05T00:00:00Z',
  }
  return {
    agent: 'eva',
    agent_name: 'Eva',
    project: 'P1',
    name: 'Connect Enterprise',
    outcome: 'A clear "what" explanation of Connect Enterprise',
    status: 'active',
    owner_name: 'Jonathan Jackson',
    links: [
      {
        label: 'Overview doc',
        url: 'https://docs.google.com/document/d/x',
        kind: '',
        waiting_on: '',
      },
    ],
    may_edit: true,
    my_person_id: null,
    built: [done],
    in_progress: [live],
    waiting: [],
    stages: { proposed: 0, started: 1, shipped: 1, declined: 0 },
    ideas: [live, done],
    contributors: [
      {
        id: 7,
        display_name: 'Sophie',
        role: '',
        source: 'summary',
        since: '2026-10-01T00:00:00Z',
        has_page: true,
        page_withheld: false,
        goal: 'Run a real RUTF procurement in Supply.',
        written_at: '2026-10-11T00:00:00Z',
        ideas: { proposed: 0, started: 1, shipped: 0, declined: 0 },
      },
    ],
  }
}

const { AgentProjectPage } = await import('./AgentProjectPage')

function task(over: Record<string, unknown> = {}) {
  return {
    agent_slug: 'eva',
    ext_id: 'T1',
    title: 'Ship the thing',
    next_action: '',
    status: 'in_progress',
    owner: '',
    assigned: 'eva',
    confidence: '',
    score: '',
    review: '',
    rationale: '',
    source_url: '',
    plan: '',
    notes: '',
    position: 1,
    batch_key: '',
    origin: '',
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    ask_kind: '',
    ask_body: '',
    ask_open: false,
    links: [],
    project_ext_id: 'P1',
    project_name: 'Connect Enterprise',
    ...over,
  }
}

function detail(over: Record<string, unknown> = {}) {
  return {
    id: 1,
    agent_slug: 'eva',
    ext_id: 'P1',
    name: 'Connect Enterprise',
    outcome: 'A clear "what" explanation of Connect Enterprise',
    status: 'active',
    owner_note: '',
    owner_email: 'jonathan@dimagi.com',
    drive_folder_id: 'abc',
    drive_folder_url: 'https://drive.google.com/drive/folders/abc',
    repo_slug: 'dimagi/connect',
    notes: '',
    links: [{ label: 'Overview doc', url: 'https://docs.google.com/document/d/x' }],
    task_count: 2,
    open_task_count: 1,
    waiting_task_count: 0,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-05T00:00:00Z',
    tasks: [task({ ext_id: 'T9', title: 'Finished one', status: 'done' }), task({ ext_id: 'T1', title: 'Live one' })],
    recent_turns: [
      {
        id: '0b5c-uuid',
        status: 'succeeded',
        prompt_preview: 'Draft the overview',
        created_at: '2026-10-04T00:00:00Z',
        task_ext_ids: ['T1'],
      },
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
  it('shows who is building it and every idea with its stage, whose words and its work', async () => {
    getProject.mockResolvedValue(detail())
    show()
    const person = await screen.findByTestId('contributor-7')
    expect(person.textContent).toContain('Sophie')
    expect(person.textContent).toContain('Run a real RUTF procurement')
    expect(person.getAttribute('href')).toBe('/w/connect/agents/eva/projects/P1/people/7')
    const idea = await screen.findByTestId('idea-T1')
    expect(within(idea).getByLabelText('Stage: Started')).toBeTruthy()
    expect(idea.textContent).toContain('the normal case is someone just ignores us')
    // Whoever runs the work sees the subtasks working on it.
    expect(idea.textContent).toContain('work: T2')
    expect(screen.getByTestId('idea-counts').textContent).toBe('1 shipped · 1 started · 0 proposed')
    expect(getProjectTeam).toHaveBeenCalledWith('eva', 'P1')
  })

  it('re-reads the project when canopy says a task moved', async () => {
    const { resourceChanged } = await import('@/widget/pageInvalidation')
    getProject.mockResolvedValue(detail())
    show()
    await screen.findByRole('heading', { name: 'Connect Enterprise' })
    expect(getProject).toHaveBeenCalledTimes(1)
    resourceChanged('task://')
    await waitFor(() => expect(getProject).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(getProjectTeam).toHaveBeenCalledTimes(2))
  })

  it('is one page: what it is, where it stands, ideas, people — then the agent’s work and activity', async () => {
    getProject.mockResolvedValue(detail())
    show()
    await screen.findByRole('heading', { name: 'Connect Enterprise' })
    expect(getProject).toHaveBeenCalledWith('eva', 'P1')

    expect(screen.getAllByRole('heading', { level: 1 }).map((h) => h.textContent)).toEqual(['Connect Enterprise'])
    const sections = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    expect(sections).toEqual(['Where it stands', 'Ideas', 'People', "Eva's work", 'Activity'])

    expect(screen.getByRole('link', { name: '← Projects' }).getAttribute('href')).toBe('/w/connect/agents/eva/projects')
    expect(screen.getByRole('link', { name: 'Overview doc' }).getAttribute('href')).toBe(
      'https://docs.google.com/document/d/x',
    )
    expect(screen.getByRole('link', { name: 'Drive folder' }).getAttribute('href')).toBe(
      'https://drive.google.com/drive/folders/abc',
    )
    expect(screen.getByText(/dimagi\/connect/)).toBeTruthy()
    // The owner is named once, in the overview; the select alone says the status.
    expect(screen.queryByText(/jonathan@dimagi\.com/)).toBeNull()
    expect(screen.getByText('Owned by Jonathan Jackson, built with Eva.')).toBeTruthy()
    // No second page to send people to.
    expect(screen.queryByText(/Team view/)).toBeNull()
  })

  it('lists each idea once: its task stays out of the work, its subtasks are in it', async () => {
    getProject.mockResolvedValue(
      detail({
        tasks: [
          task({ ext_id: 'T5', title: 'An idea', kind: 'idea' }),
          task({ ext_id: 'T6', title: 'Work toward it', parent_ext_id: 'T5' }),
          task({ ext_id: 'T1', title: 'Live one' }),
        ],
      }),
    )
    show()
    await screen.findByTestId('task-T1')
    const cards = screen.getAllByTestId(/^task-T/).map((el) => el.getAttribute('data-testid'))
    expect(cards).toEqual(['task-T6', 'task-T1'])
  })

  it('lists live tasks before finished ones, and turns link to the Turns page', async () => {
    getProject.mockResolvedValue(detail())
    show()
    await screen.findByTestId('task-T1')
    const cards = screen.getAllByTestId(/^task-T/).map((el) => el.getAttribute('data-testid'))
    expect(cards).toEqual(['task-T1', 'task-T9'])
    // Finished work folds away under its count, closed, so it can't pass for open.
    const finished = screen.getByTestId('finished-work') as HTMLDetailsElement
    expect(finished.open).toBe(false)
    expect(within(finished).getByText('Done (1)')).toBeTruthy()
    expect(within(finished).getByTestId('task-T9')).toBeTruthy()
    expect(within(finished).queryByTestId('task-T1')).toBeNull()

    const turn = screen.getByTestId('turn-0b5c-uuid')
    expect(turn.getAttribute('href')).toBe('/w/connect/agents/eva/turns#0b5c-uuid')
    expect(turn.textContent).toContain('Draft the overview')
    expect(turn.textContent).toContain('succeeded')
  })

  it("shows each section's empty state", async () => {
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
    await waitFor(() =>
      expect(patchProject).toHaveBeenCalledWith('eva', 'P1', {
        status: 'done',
      }),
    )
    await waitFor(() => expect(getProject).toHaveBeenCalledTimes(2))
  })

  it('a teammate who cannot edit sees the overview, not the agent’s console', async () => {
    ctx.canEdit = false
    getProject.mockResolvedValue(detail())
    getProjectTeam.mockResolvedValueOnce({ ...teamView(), may_edit: false })
    show()
    await screen.findByTestId('idea-T1')
    const sections = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    expect(sections).toEqual(['Where it stands', 'Ideas', 'People'])
    expect(screen.queryByLabelText('Project status')).toBeNull()
    expect(screen.queryByTestId(/^task-T/)).toBeNull()
    expect(screen.getByTestId('project-overview').textContent).not.toMatch(/\bT\d+\b/)
  })

  it('/w/x/agents/eva/projects/P1 routes to the project page', async () => {
    const { createMemoryRouter, matchRoutes, Outlet, RouterProvider } = await import('react-router-dom')
    const { routeTable } = await import('@/router')
    const matches = matchRoutes(routeTable, '/w/x/agents/eva/projects/P1') ?? []
    expect(matches.at(-1)?.route.path).toBe('projects/:ref')
    expect(matches.at(-1)?.params).toMatchObject({
      workspace: 'x',
      slug: 'eva',
      ref: 'P1',
    })

    // Render the agent route's REAL children (lazy + LazySection) under a bare
    // outlet — the shell itself needs auth/workspace providers this test omits.
    const agentRoute = matches.at(-2)?.route
    getProject.mockResolvedValue(detail())
    const router = createMemoryRouter(
      [
        {
          path: '/w/:workspace/agents/:slug',
          element: <Outlet />,
          children: agentRoute?.children,
        },
      ],
      { initialEntries: ['/w/x/agents/eva/projects/P1'] },
    )
    render(<RouterProvider router={router} />)
    // The route is lazy: under a full parallel run the chunk import alone can
    // outlast findBy's 1s default.
    expect(await screen.findByRole('heading', { name: 'Connect Enterprise' }, { timeout: 5000 })).toBeTruthy()
    expect(getProject).toHaveBeenCalledWith('eva', 'P1')
  })

  it('the old team-view address lands on the project page, a person’s on theirs', async () => {
    const { ProjectPageRedirect } = await import('@/router')
    const landed = (path: string) =>
      render(
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/w/:workspace/projects/:agent/:ref" element={<ProjectPageRedirect />} />
            <Route path="/w/:workspace/projects/:agent/:ref/people/:person" element={<ProjectPageRedirect />} />
            <Route path="/w/:workspace/agents/:slug/projects/:ref" element={<p>project page</p>} />
            <Route path="/w/:workspace/agents/:slug/projects/:ref/people/:person" element={<p>person page</p>} />
          </Routes>
        </MemoryRouter>,
      )
    landed('/w/connect/projects/hal/P5')
    expect(await screen.findByText('project page')).toBeTruthy()
    cleanup()
    landed('/w/connect/projects/hal/P5/people/7')
    expect(await screen.findByText('person page')).toBeTruthy()
  })
})
