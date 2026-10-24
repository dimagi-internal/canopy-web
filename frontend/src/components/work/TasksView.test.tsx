// @vitest-environment jsdom
//
// The task list body: what each tab asks the server, that every filter is the
// URL, saving a view, a bulk change that partly fails, the keyboard, a board
// drop's neighbours and the calendar's day.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

const api = vi.hoisted(() => ({
  listTasks: vi.fn<(q?: unknown, ws?: string) => Promise<unknown[]>>(async () => []),
  countTasks: vi.fn(async () => 0),
  getTaskSettings: vi.fn(async () => ({ workspace: 'dim', prefix: 'DIM', status_names: {}, update_cadence_days: 7 })),
  listViews: vi.fn<() => Promise<unknown[]>>(async () => []),
  createView: vi.fn(async (body: Record<string, unknown>) => ({ id: 7, mine: true, rank: 0, ...body })),
  updateView: vi.fn(),
  deleteView: vi.fn(),
  bulkTasks: vi.fn<(keys: string[], change: unknown) => Promise<unknown[]>>(async () => []),
  updateTask: vi.fn(async (key: string, body: Record<string, unknown>) => ({ ...task({ key }), ...body })),
  createTask: vi.fn(),
  actOn: vi.fn(),
}))

vi.mock('@/api/work', async (orig) => ({ ...(await orig<typeof import('@/api/work')>()), ...api }))
vi.mock('./useDirectory', () => ({
  useDirectory: () => ({ people: [], agents: [], labels: [{ id: 1, name: 'bug', color: 'destructive' }], projects: [] }),
  invalidateDirectory: () => {},
}))
vi.mock('./TaskDetailView', () => ({ TaskDetailView: ({ taskKey }: { taskKey: string }) => <div>detail {taskKey}</div> }))
vi.mock('@/widget/usePageState', () => ({ usePageState: () => {} }))
vi.mock('@/workspace/WorkspaceProvider', () => ({ useWorkspaceList: () => [{ slug: 'dim', role: 'owner' }] }))

const { TasksView } = await import('./TasksView')
const { placement } = await import('./TaskBoard')

function task(over: Record<string, unknown> = {}) {
  return {
    id: 1, key: 'DIM-1', url: '', workspace: 'dim', ext_id: 'T1', kind: 'work', title: 'Ship it',
    next_action: '', status: 'todo', category: 'unstarted', priority: 0, labels: [], owner: '',
    assigned: '', agent_slug: null, assignee_email: null, ask_kind: '', ask_body: '', ask_open: false,
    batch_key: '', idempotency_key: '', origin: '', confidence: '', score: '', review: '', rationale: '',
    source_url: '', plan: '', due: null, notes: '', position: 0, rank: 0, drive_folder_id: '',
    drive_folder_url: '', created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z',
    ...over,
  }
}

function Where() {
  const loc = useLocation()
  return <div data-testid="where">{loc.search}</div>
}

function show(search = '', props: Partial<Parameters<typeof TasksView>[0]> = {}) {
  return render(
    <MemoryRouter initialEntries={[`/w/dim/tasks${search}`]}>
      <Routes>
        <Route path="/w/:workspace/tasks" element={<><TasksView workspace="dim" tabs {...props} /><Where /></>} />
      </Routes>
    </MemoryRouter>,
  )
}

const where = () => new URLSearchParams(screen.getByTestId('where').textContent ?? '')
const lastQuery = () => api.listTasks.mock.calls.at(-1)?.[0] as Record<string, unknown>

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  api.listTasks.mockImplementation(async () => [])
})

describe('TasksView tabs', () => {
  it('maps each tab to its query, open work by default', async () => {
    api.listTasks.mockResolvedValue([task()])
    show()
    await waitFor(() => expect(lastQuery()).toEqual({ assignee: 'me', category: 'triage,backlog,unstarted,started' }))
    fireEvent.click(screen.getByTestId('tab-waiting'))
    await waitFor(() => expect(lastQuery()).toMatchObject({ waiting: 'me' }))
    fireEvent.click(screen.getByTestId('tab-created'))
    await waitFor(() => expect(lastQuery()).toMatchObject({ created_by: 'me' }))
    fireEvent.click(screen.getByTestId('tab-all'))
    await waitFor(() => expect(lastQuery()).toEqual({ category: 'triage,backlog,unstarted,started' }))
    expect(where().get('tab')).toBe('all')
  })

  it('an empty My tasks on a first visit falls back to All, once', async () => {
    show()
    await waitFor(() => expect(where().get('tab')).toBe('all'))
    expect(screen.getByTestId('tab-all').getAttribute('aria-selected')).toBe('true')
  })

  it('an old ?waiting=me link opens Waiting on me', async () => {
    show('?waiting=me')
    await waitFor(() => expect(lastQuery()).toMatchObject({ waiting: 'me' }))
    expect(screen.getByTestId('tab-waiting').getAttribute('aria-selected')).toBe('true')
  })

  it('counts each tab', async () => {
    api.countTasks.mockResolvedValue(4)
    show('?tab=all')
    await waitFor(() => expect(screen.getByTestId('tab-mine').textContent).toContain('4'))
    expect(api.countTasks).toHaveBeenCalledWith(
      { assignee: 'me', category: 'triage,backlog,unstarted,started' }, 'dim')
  })

  it('pinned filters win over the URL', async () => {
    show('?tab=all', { fixed: { agent: 'eva' }, tabs: ['all', 'waiting'] })
    await waitFor(() => expect(lastQuery()).toMatchObject({ agent: 'eva' }))
    expect(screen.queryByTestId('tab-mine')).toBeNull()
  })
})

describe('TasksView filters', () => {
  it('every filter is written to the URL and read back into the query', async () => {
    show('?tab=all')
    await waitFor(() => expect(api.listTasks).toHaveBeenCalled())
    fireEvent.click(screen.getByTestId('category-completed'))
    await waitFor(() => expect(where().get('category')).toBe('triage,backlog,unstarted,started,completed'))
    fireEvent.change(screen.getByLabelText('Priority'), { target: { value: '1' } })
    fireEvent.change(screen.getByLabelText('Label'), { target: { value: 'bug' } })
    fireEvent.change(screen.getByLabelText('Kind'), { target: { value: 'idea' } })
    fireEvent.change(screen.getByLabelText('Group by'), { target: { value: 'priority' } })
    fireEvent.change(screen.getByLabelText('Sort'), { target: { value: 'due' } })
    await waitFor(() => expect(where().get('sort')).toBe('due'))
    expect(Object.fromEntries(where())).toMatchObject({ priority: '1', label: 'bug', kind: 'idea', group: 'priority', tab: 'all' })
    expect(lastQuery()).toMatchObject({ priority: '1', label: 'bug', kind: 'idea', sort: 'due' })
    fireEvent.click(screen.getByRole('radio', { name: 'Board' }))
    await waitFor(() => expect(where().get('layout')).toBe('board'))
    expect(screen.getByTestId('task-board')).toBeTruthy()
  })

  it('overdue asks for tasks due before today', async () => {
    show('?tab=all&due=overdue')
    await waitFor(() => expect(lastQuery()).toHaveProperty('due_before'))
    expect(lastQuery()).not.toHaveProperty('due_after')
  })
})

describe('saved views', () => {
  it('saves what the list shows, and opens it', async () => {
    show('?tab=waiting&priority=2&group=project')
    await waitFor(() => expect(api.listViews).toHaveBeenCalled())
    fireEvent.click(screen.getByRole('button', { name: 'Save view…' }))
    fireEvent.change(await screen.findByLabelText('View name'), { target: { value: 'Urgent asks' } })
    fireEvent.click(screen.getByLabelText('Share with the workspace'))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.createView).toHaveBeenCalledWith({
      workspace: 'dim', name: 'Urgent asks', shared: true,
      filters: { tab: 'waiting', priority: '2' }, group_by: 'project', sort: '', layout: 'table',
    }))
    await waitFor(() => expect(where().get('saved')).toBe('7'))
    expect(where().get('priority')).toBe('2')
  })

  it('picking a view replaces the URL with its parameters', async () => {
    api.listViews.mockResolvedValue([{
      id: 3, workspace: 'dim', name: 'Board', shared: true, mine: false, owner_email: 'a@x.org',
      filters: { tab: 'all', label: 'bug' }, group_by: '', sort: '', layout: 'board', rank: 0,
    }])
    show('?tab=mine&priority=1')
    await waitFor(() => expect(screen.getByRole('option', { name: /Board · a/ })).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Saved view'), { target: { value: '3' } })
    await waitFor(() => expect(where().get('saved')).toBe('3'))
    expect(Object.fromEntries(where())).toEqual({ tab: 'all', label: 'bug', layout: 'board', saved: '3' })
  })
})

describe('bulk changes', () => {
  it('reports each task the server refused', async () => {
    api.listTasks.mockResolvedValue([task({ key: 'DIM-1' }), task({ key: 'DIM-2', title: 'Two' })])
    api.bulkTasks.mockResolvedValue([
      { key: 'DIM-1', ok: true, error: '', task: task({ key: 'DIM-1', status: 'done' }) },
      { key: 'DIM-2', ok: false, error: 'not yours to change', task: null },
    ])
    show('?tab=all')
    fireEvent.click(await screen.findByLabelText('Select DIM-1'))
    fireEvent.click(screen.getByLabelText('Select DIM-2'))
    const bar = screen.getByRole('region', { name: 'Change selected tasks' })
    expect(within(bar).getByText('2 selected')).toBeTruthy()
    fireEvent.click(within(bar).getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(api.bulkTasks).toHaveBeenCalledWith(['DIM-1', 'DIM-2'], { action: { action: 'done' } }))
    const alert = await within(bar).findByRole('alert')
    expect(alert.textContent).toContain('1 not changed')
    expect(alert.textContent).toContain('DIM-2')
    expect(alert.textContent).toContain('not yours to change')
  })

  it('adding a label keeps each task’s own labels', async () => {
    api.listTasks.mockResolvedValue([
      task({ key: 'DIM-1', labels: [{ name: 'ux', color: 'info' }] }),
      task({ key: 'DIM-2' }),
    ])
    api.bulkTasks.mockImplementation(async (keys: string[]) => keys.map((key) => ({ key, ok: true, error: '', task: null })))
    show('?tab=all')
    fireEvent.click(await screen.findByLabelText('Select DIM-1'))
    fireEvent.click(screen.getByLabelText('Select DIM-2'))
    fireEvent.change(screen.getByLabelText('Add label'), { target: { value: 'bug' } })
    await waitFor(() => expect(api.bulkTasks).toHaveBeenCalledTimes(2))
    expect(api.bulkTasks).toHaveBeenCalledWith(['DIM-1'], { patch: { labels: ['ux', 'bug'] } })
    expect(api.bulkTasks).toHaveBeenCalledWith(['DIM-2'], { patch: { labels: ['bug'] } })
  })
})

describe('keyboard', () => {
  it('J/K move, X selects (Shift+X a range), Space opens the peek, Esc clears', async () => {
    api.listTasks.mockResolvedValue([task({ key: 'DIM-1' }), task({ key: 'DIM-2' }), task({ key: 'DIM-3' })])
    show('?tab=all')
    await screen.findByLabelText('Select DIM-3')
    const row = (k: string) => document.querySelector(`tr[data-task="${k}"]`) as HTMLElement
    fireEvent.keyDown(window, { key: 'j' })
    fireEvent.keyDown(window, { key: 'j' })
    expect(row('DIM-2').className).toContain('outline')
    fireEvent.keyDown(window, { key: 'k' })
    expect(row('DIM-1').className).toContain('outline')
    fireEvent.keyDown(window, { key: 'x' })
    expect(row('DIM-1').getAttribute('aria-selected')).toBe('true')
    fireEvent.keyDown(window, { key: 'j' })
    fireEvent.keyDown(window, { key: 'j' })
    fireEvent.keyDown(window, { key: 'X', shiftKey: true })
    expect(['DIM-1', 'DIM-2', 'DIM-3'].map((k) => row(k).getAttribute('aria-selected'))).toEqual(['true', 'true', 'true'])
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(row('DIM-2').getAttribute('aria-selected')).toBe('false')
    // A modifier leaves the key to the browser.
    fireEvent.keyDown(window, { key: 'x', metaKey: true })
    expect(row('DIM-3').getAttribute('aria-selected')).toBe('false')
    fireEvent.keyDown(window, { key: ' ' })
    await waitFor(() => expect(where().get('peek')).toBe('DIM-3'))
    expect(screen.getByTestId('task-peek').textContent).toContain('detail DIM-3')
  })

  it('ignores keys typed into a field', async () => {
    api.listTasks.mockResolvedValue([task({ key: 'DIM-1' })])
    show('?tab=all')
    await screen.findByLabelText('Select DIM-1')
    fireEvent.keyDown(screen.getByLabelText('Search tasks'), { key: 'j' })
    expect((document.querySelector('tr[data-task="DIM-1"]') as HTMLElement).className).not.toContain('outline')
  })
})

describe('board', () => {
  it('a drop names the card above as before and the card below as after', () => {
    expect(placement(['A', 'B', 'C'], 'C', 1)).toEqual({ before: 'A', after: 'B' })
    expect(placement(['A', 'B', 'C'], 'A', 0)).toEqual({ after: 'B' })
    expect(placement(['A', 'B'], 'X', 2)).toEqual({ before: 'B' })
    expect(placement([], 'X', 0)).toEqual({})
  })

  it('dragging a card onto another column sends its status and neighbours', async () => {
    api.listTasks.mockResolvedValue([
      task({ key: 'DIM-1', status: 'todo' }),
      task({ key: 'DIM-2', status: 'in_progress' }),
      task({ key: 'DIM-3', status: 'in_progress' }),
    ])
    show('?tab=all&layout=board')
    await screen.findByTestId('task-board')
    expect(lastQuery()).toMatchObject({ sort: 'rank' })
    const card = (k: string) => document.querySelector(`[data-task="${k}"]`) as HTMLElement
    await waitFor(() => expect(card('DIM-1')).toBeTruthy())
    fireEvent.dragStart(card('DIM-1'))
    // jsdom has no layout, so a card's midpoint is 0: the pointer is "below" DIM-2.
    fireEvent.dragOver(card('DIM-2'), { clientY: 1 })
    fireEvent.drop(card('DIM-2'))
    await waitFor(() => expect(api.updateTask).toHaveBeenCalledWith('DIM-1', { status: 'in_progress', before: 'DIM-2', after: 'DIM-3' }))
  })
})

describe('calendar', () => {
  it('puts a task on its due day and an undated one beside the month', async () => {
    api.listTasks.mockResolvedValue([
      task({ key: 'DIM-1', title: 'Dated', due: '2026-10-15' }),
      task({ key: 'DIM-2', title: 'Undated' }),
    ])
    show('?tab=all&layout=calendar&month=2026-10')
    await screen.findByTestId('task-calendar')
    const day = document.querySelector('[data-date="2026-10-15"]') as HTMLElement
    await waitFor(() => expect(within(day).getByText('Dated')).toBeTruthy())
    expect(within(screen.getByTestId('undated')).getByText('Undated')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Next month' }))
    await waitFor(() => expect(where().get('month')).toBe('2026-11'))
  })
})

describe('edits', () => {
  it('a refused edit puts the row back and says why', async () => {
    api.listTasks.mockResolvedValue([task({ key: 'DIM-1' })])
    api.updateTask.mockRejectedValueOnce(new Error('needs the editor role'))
    show('?tab=all')
    const due = await screen.findByLabelText('Due date')
    await act(async () => {
      fireEvent.change(due, { target: { value: '2026-11-01' } })
    })
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('needs the editor role')
    fireEvent.click(within(alert).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
