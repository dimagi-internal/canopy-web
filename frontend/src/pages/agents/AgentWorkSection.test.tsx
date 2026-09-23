// @vitest-environment jsdom
//
// Work is one page over one table. Tasks, Items and Projects were three rail
// entries rendering the same `AgentTask` rows — an item has been a property of
// a task since #873, and `/items/` has served tasks ever since — so a task
// carrying an ask appeared on three screens at once and the board, which reads
// none of the ask fields, showed it as ordinary work in flight.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

const { listAgentTasks, listAgentCommands, listAgentProjects, decideItem } = vi.hoisted(() => ({
  listAgentTasks: vi.fn(),
  listAgentCommands: vi.fn<() => Promise<unknown[]>>(async () => []),
  listAgentProjects: vi.fn<() => Promise<unknown[]>>(async () => []),
  decideItem: vi.fn(async () => ({})),
}))

vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  listAgentTasks, listAgentCommands, listAgentProjects,
}))
vi.mock('@/api/items', () => ({ decideItem, listItems: vi.fn(async () => []) }))
vi.mock('@/pages/agents/QuickTurn', () => ({ QuickTurn: () => <div>quick-turn</div> }))
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useOutletContext: () => ({ agent: { slug: 'hal', name: 'Hal', workspace: 'connect' } }),
}))

const { AgentWorkSection } = await import('./AgentWorkSection')
const { WorkRedirect } = await import('./WorkRedirect')

function task(over: Record<string, unknown> = {}) {
  return {
    id: 1, agent_slug: 'hal', ext_id: 'T1', uuid: 'u-1', title: 'Ship the thing',
    next_action: '', status: 'in_progress', owner: '', assigned: 'Hal', confidence: '',
    score: '', review: '', rationale: '', source_url: '', plan: '', notes: '', position: 1,
    updated_at: '2026-09-23T00:00:00Z', ask_kind: '', ask_state: '', links: [],
    project_ext_id: null, project_name: null, ...over,
  }
}

function show(entry = '/w/connect/agents/hal/work') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes><Route path="/w/:workspace/agents/:slug/work" element={<AgentWorkSection />} /></Routes>
    </MemoryRouter>,
  )
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('AgentWorkSection', () => {
  it('shows an ask on the card, where the work is', async () => {
    listAgentTasks.mockResolvedValue([task({ ask_kind: 'review', ask_state: 'open' })])
    show()
    await waitFor(() => expect(screen.getByTestId('ask-T1')).toBeTruthy())
    expect(screen.getByTestId('ask-T1').textContent).toContain('review')
  })

  it('decides an ask from the card and reloads', async () => {
    listAgentTasks.mockResolvedValue([task({ ask_kind: 'review', ask_state: 'open' })])
    show()
    await waitFor(() => expect(screen.getByTestId('ask-implement-T1')).toBeTruthy())
    fireEvent.click(screen.getByTestId('ask-implement-T1'))
    await waitFor(() => expect(decideItem).toHaveBeenCalledWith('u-1', 'implement'))
    expect(listAgentTasks.mock.calls.length).toBeGreaterThan(1)   // reloaded
  })

  it('leaves a task with no open ask alone', async () => {
    listAgentTasks.mockResolvedValue([
      task({ ask_kind: 'review', ask_state: 'decided' }),
      task({ id: 2, ext_id: 'T2', uuid: 'u-2' }),
    ])
    show()
    await waitFor(() => expect(screen.getByTestId('task-T2')).toBeTruthy())
    expect(screen.queryByTestId('ask-T1')).toBeNull()
    expect(screen.queryByTestId('ask-T2')).toBeNull()
  })

  it('hides settled work until asked, then shows it', async () => {
    listAgentTasks.mockResolvedValue([
      task(), task({ id: 2, ext_id: 'T2', uuid: 'u-2', status: 'done' }),
    ])
    show()
    await waitFor(() => expect(screen.getByTestId('task-T1')).toBeTruthy())
    expect(screen.queryByTestId('task-T2')).toBeNull()
    fireEvent.click(screen.getByTestId('show-settled'))
    await waitFor(() => expect(screen.getByTestId('task-T2')).toBeTruthy())
  })

  it('groups by project, keeping tasks with no project', async () => {
    listAgentProjects.mockResolvedValue([
      { ext_id: 'P1', name: 'UNGA 2026', status: 'active', outcome: '', task_count: 1,
        open_task_count: 1, drive_folder_url: 'https://drive/x', repo_slug: '' },
    ])
    listAgentTasks.mockResolvedValue([
      task({ project_ext_id: 'P1', project_name: 'UNGA 2026' }),
      task({ id: 2, ext_id: 'T2', uuid: 'u-2' }),
    ])
    show('/w/connect/agents/hal/work?by=project')
    await waitFor(() => expect(screen.getByTestId('project-P1')).toBeTruthy())
    // The project's own facts, which a task cannot carry.
    const group = screen.getByTestId('project-P1')
    expect(within(group).getByRole('link', { name: 'Drive folder' })).toBeTruthy()
    expect(within(group).getByTestId('task-T1')).toBeTruthy()
    // A task with no project is still work, so it is still on the page.
    expect(within(screen.getByTestId('project-none')).getByTestId('task-T2')).toBeTruthy()
  })

  it('keeps ?batch= working — one sitting is a link people hold', async () => {
    listAgentTasks.mockResolvedValue([task()])
    show('/w/connect/agents/hal/work?batch=fleet-audit-9')
    // The board is not what a batch link asks for; the sitting is.
    await waitFor(() => expect(screen.queryByTestId('task-T1')).toBeNull())
  })
})

describe('WorkRedirect', () => {
  it('sends /items to Work with its query intact', () => {
    function Where() {
      const l = useLocation()
      return <div data-testid="where">{l.pathname + l.search}</div>
    }
    render(
      <MemoryRouter initialEntries={['/w/connect/agents/hal/items?batch=fleet-audit-9']}>
        <Routes>
          <Route path="/w/:workspace/agents/:slug">
            <Route path="items" element={<WorkRedirect />} />
            <Route path="work" element={<Where />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.getByTestId('where').textContent)
      .toBe('/w/connect/agents/hal/work?batch=fleet-audit-9')
  })
})
