// @vitest-environment jsdom
//
// A project's overview — the top of its one page, all a teammate needs. What
// it is, where it stands, the ideas (and adding yours), the people — and none
// of the agent's process: no task numbers unless asked, no approve/decline/nudge.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const { getProjectTeam, addIdea, actOnTask } = vi.hoisted(() => ({
  getProjectTeam: vi.fn<(slug: string, ref: string) => Promise<unknown>>(),
  addIdea: vi.fn<(slug: string, ref: string, body: { quote: string }) => Promise<unknown>>(),
  actOnTask: vi.fn(async () => ({})),
}))

vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getProjectTeam,
  addIdea,
  actOnTask,
}))

const { ProjectOverview } = await import('./ProjectOverview')

function idea(over: Record<string, unknown>) {
  return {
    ext_id: 'I1',
    title: 'Sophie can run her real RUTF procurement',
    quote: 'the goal here IS something sophie can start using',
    who: { person_id: 7, name: 'Jonathan Jackson' },
    source_url: 'https://canopy.test/chat/1',
    said_at: '2026-09-28T00:00:00Z',
    stage: 'started',
    status: 'in_progress',
    people: [7],
    subtasks: [{ ext_id: 'T59', title: 'Screen review', status: 'done' }],
    created_at: '2026-10-12T00:00:00Z',
    updated_at: '2026-10-12T00:00:00Z',
    ...over,
  }
}

function team(over: Record<string, unknown> = {}) {
  const ideas = [
    idea({ ext_id: 'T71' }),
    idea({
      ext_id: 'T90',
      title: 'Per-org Labs pages',
      stage: 'proposed',
      status: 'suggested',
      subtasks: [],
    }),
  ]
  return {
    agent: 'hal',
    agent_name: 'Hal',
    project: 'P5',
    name: 'Supply',
    outcome: 'One shared record of what was ordered and how much is left in each worker’s hands.',
    status: 'active',
    owner_name: 'Jonathan Jackson',
    links: [
      {
        label: 'Live app',
        url: 'https://labs.test/supply/',
        kind: '',
        waiting_on: '',
      },
    ],
    may_edit: false,
    my_person_id: 12,
    built: [],
    in_progress: [ideas[0]],
    waiting: [
      {
        text: 'How many days of ration the forecast assumes',
        url: 'https://github.test/2386',
        waiting_on: 'Jonathan Jackson',
      },
    ],
    stages: { proposed: 1, started: 1, shipped: 0, declined: 0 },
    ideas,
    contributors: [
      {
        id: 7,
        display_name: 'Jonathan Jackson',
        role: '',
        source: 'idea',
        since: '2026-10-12T00:00:00Z',
        has_page: false,
        page_withheld: false,
        goal: '',
        written_at: null,
        ideas: { proposed: 1, started: 1, shipped: 0, declined: 0 },
      },
    ],
    ...over,
  }
}

function show(showRefs = false) {
  return render(
    <MemoryRouter>
      <ProjectOverview
        agent="hal"
        projectRef="P5"
        pageHref={(id) => `/w/connect/agents/hal/projects/P5/people/${id}`}
        showRefs={showRefs}
      />
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ProjectOverview', () => {
  it('says what it is, where it stands, the ideas, the people — in that order', async () => {
    getProjectTeam.mockResolvedValue(team())
    show()
    expect((await screen.findByRole('heading', { level: 1 })).textContent).toBe('Supply')
    expect(getProjectTeam).toHaveBeenCalledWith('hal', 'P5')
    expect(screen.getByTestId('team-outcome').textContent).toContain('One shared record')
    expect(screen.getByRole('link', { name: 'Live app' }).getAttribute('href')).toBe('https://labs.test/supply/')
    const sections = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    expect(sections).toEqual(['Where it stands', 'Ideas', 'People'])
  })

  it('labels the links, and says the standing counts ideas, not the product', async () => {
    getProjectTeam.mockResolvedValue(team())
    show()
    expect((await screen.findByTestId('team-links')).textContent).toMatch(/^Links/)
    expect(screen.getByText('Ideas shipped')).toBeTruthy()
    expect(screen.getByText('None of the ideas below is shipped yet.')).toBeTruthy()
    expect(screen.getByText('Ideas in progress')).toBeTruthy()
  })

  it('leads where it stands with what is live today, kept out of the link row', async () => {
    getProjectTeam.mockResolvedValue(
      team({
        links: [
          { label: 'Live app', url: 'https://labs.test/supply/', kind: '', waiting_on: '' },
          { label: 'Stock review and forecasts per programme', url: 'https://labs.test/supply/10112/',
            kind: 'live', waiting_on: '' },
        ],
      }),
    )
    show()
    const live = await screen.findByTestId('team-live')
    expect(live.textContent).toBe('Live todayStock review and forecasts per programme')
    expect(screen.getByTestId('team-links').textContent).not.toContain('Stock review')
  })

  it('lists each open decision as one line with who it waits on', async () => {
    getProjectTeam.mockResolvedValue(team())
    show()
    const list = await screen.findByTestId('team-decisions')
    expect(list.textContent).toContain('How many days of ration the forecast assumes')
    expect(list.textContent).toContain('waiting on Jonathan Jackson')
  })

  it('shows none of the agent’s process to a teammate: no task numbers, no approve', async () => {
    getProjectTeam.mockResolvedValue(team())
    show()
    const view = await screen.findByTestId('project-overview')
    expect(view.textContent).not.toMatch(/\bT\d+\b/)
    expect(screen.queryByRole('button', { name: /Approve|Decline|Nudge/ })).toBeNull()
    // Not their idea, and not their project to run: nothing to mark.
    expect(screen.queryByTestId('mark-shipped')).toBeNull()
  })

  it('lets the person whose idea it is mark it shipped', async () => {
    getProjectTeam.mockResolvedValue(team({ my_person_id: 7 }))
    show()
    await screen.findByTestId('project-overview')
    expect(screen.getAllByTestId('mark-shipped')).toHaveLength(2)
  })

  it('asks before marking an idea shipped', async () => {
    getProjectTeam.mockResolvedValue(team({ my_person_id: 7 }))
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
    show()
    await screen.findByTestId('project-overview')
    fireEvent.click(screen.getAllByTestId('mark-shipped')[0])
    expect(actOnTask).not.toHaveBeenCalled()
    fireEvent.click(screen.getAllByTestId('mark-shipped')[0])
    expect(actOnTask).toHaveBeenCalledWith('hal', 'T71', 'done')
    confirm.mockRestore()
  })

  it('names each idea’s task and its work for whoever runs it', async () => {
    getProjectTeam.mockResolvedValue(team({ may_edit: true }))
    show(true)
    const view = await screen.findByTestId('project-overview')
    expect(view.textContent).toContain('T71')
    expect(view.textContent).toContain('work: T59')
  })

  it('takes your idea in your words and says what happens next', async () => {
    getProjectTeam.mockResolvedValue(team())
    addIdea.mockResolvedValue({
      idea: idea({ ext_id: 'T95' }),
      notified: ['Hal', 'Jonathan Jackson'],
    })
    show()
    fireEvent.click(await screen.findByTestId('add-idea'))
    const form = screen.getByTestId('add-idea-form')
    fireEvent.change(within(form).getByRole('textbox'), {
      target: { value: 'Show me who never replied' },
    })
    fireEvent.click(within(form).getByRole('button', { name: 'Add idea' }))
    const done = await screen.findByTestId('idea-added')
    expect(addIdea).toHaveBeenCalledWith('hal', 'P5', {
      quote: 'Show me who never replied',
    })
    expect(done.textContent).toContain('Proposed')
    expect(done.textContent).toContain('We told Hal and Jonathan Jackson')
    expect(getProjectTeam).toHaveBeenCalledTimes(2)
  })

  it('says a person without a page has not shared what they are after', async () => {
    getProjectTeam.mockResolvedValue(team())
    show()
    const person = await screen.findByTestId('contributor-7')
    expect(person.getAttribute('href')).toBe('/w/connect/agents/hal/projects/P5/people/7')
    expect(person.textContent).toContain("Jonathan hasn't shared what they're after yet.")
    expect(person.textContent).toContain('0 shipped · 1 started · 1 proposed')
  })
})
