// @vitest-environment jsdom
//
// Done stays in place: a task finished in the list keeps its row (struck through,
// with Undo) through the re-read that would drop it, until the list's own query
// changes. The status picker is a plain select here; its menu is canopy-ui's.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

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
vi.mock('./bits', async (orig) => ({
  ...(await orig<typeof import('./bits')>()),
  StatusPicker: ({ status, onChange }: { status: string; onChange: (s: string) => void }) => (
    <select aria-label="Status" value={status} onChange={(e) => onChange(e.target.value)}>
      {['backlog', 'todo', 'in_progress', 'done', 'declined'].map((v) => <option key={v} value={v}>{v}</option>)}
    </select>
  ),
}))
vi.mock('@/workspace/WorkspaceProvider', () => ({ useWorkspaceList: () => [{ slug: 'dim', role: 'owner' }] }))

const { TasksView } = await import('./TasksView')
const { resourceChanged } = await import('@/widget/pageInvalidation')
const { TASK_RESOURCE } = await import('@/widget/useResource')

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

function show(search = '') {
  return render(
    <MemoryRouter initialEntries={[`/w/dim/tasks${search}`]}>
      <Routes>
        <Route path="/w/:workspace/tasks" element={<TasksView workspace="dim" tabs />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  api.listTasks.mockImplementation(async () => [])
})

describe('TasksView done stays in place', () => {
  async function finish(key: string) {
    const row = (await screen.findByText('Ship it')).closest('[data-task]') as HTMLElement
    expect(row.getAttribute('data-task')).toBe(key)
    await act(async () => {
      fireEvent.change(within(row).getByLabelText('Status'), { target: { value: 'done' } })
    })
  }

  it('a finished task keeps its row, struck through, through a re-read that drops it, and Undo reopens it', async () => {
    api.listTasks.mockResolvedValue([task({ key: 'DIM-1' }), task({ key: 'DIM-2', title: 'Next one' })])
    show('?tab=all')
    await finish('DIM-1')
    await waitFor(() => expect(api.updateTask).toHaveBeenCalledWith('DIM-1', { status: 'done' }))
    // The server's open-work list no longer has it; the list re-reads when any task moves.
    api.listTasks.mockResolvedValue([task({ key: 'DIM-2', title: 'Next one' })])
    await act(async () => {
      resourceChanged(TASK_RESOURCE)
      await new Promise((r) => setTimeout(r, 700))
    })
    await waitFor(() => expect(api.listTasks.mock.calls.length).toBeGreaterThan(1))
    const title = screen.getByText('Ship it')
    expect(title.className).toContain('line-through')
    // Still above the task that followed it.
    const rows = Array.from(document.querySelectorAll('[data-task]')).map((r) => r.getAttribute('data-task'))
    expect(rows).toEqual(['DIM-1', 'DIM-2'])
    await act(async () => {
      fireEvent.click(screen.getByTestId('undo-DIM-1'))
    })
    await waitFor(() => expect(api.updateTask).toHaveBeenLastCalledWith('DIM-1', { status: 'todo' }))
    expect(screen.queryByTestId('undo-DIM-1')).toBeNull()
    expect(screen.getByText('Ship it').className).not.toContain('line-through')
  })

  it('changing the filters lets a finished task go', async () => {
    api.listTasks.mockResolvedValue([task({ key: 'DIM-1' })])
    show('?tab=all')
    await finish('DIM-1')
    await waitFor(() => expect(screen.getByTestId('undo-DIM-1')).toBeTruthy())
    api.listTasks.mockResolvedValue([])
    fireEvent.click(screen.getByTestId('category-backlog'))
    await waitFor(() => expect(screen.queryByText('Ship it')).toBeNull())
  })

  it('one tap on the circle finishes it in place, and a second tap puts it back', async () => {
    api.listTasks.mockResolvedValue([task({ key: 'DIM-1', status: 'in_progress', category: 'started' })])
    show('?tab=all')
    const circle = await screen.findByRole('button', { name: 'Mark DIM-1 done' })
    await act(async () => {
      fireEvent.click(circle)
    })
    await waitFor(() => expect(api.updateTask).toHaveBeenCalledWith('DIM-1', { status: 'done' }))
    api.listTasks.mockResolvedValue([])
    await act(async () => {
      resourceChanged(TASK_RESOURCE)
      await new Promise((r) => setTimeout(r, 700))
    })
    expect(screen.getByText('Ship it').className).toContain('line-through')
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Reopen DIM-1' }))
    })
    // Back to what it was, not just todo.
    await waitFor(() => expect(api.updateTask).toHaveBeenLastCalledWith('DIM-1', { status: 'in_progress' }))
  })
})

