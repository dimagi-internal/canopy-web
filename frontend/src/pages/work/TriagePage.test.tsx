// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

import type { TriageEntry } from '@/api/work'

const listTriage = vi.fn<(q: { project?: string }, ws?: string) => Promise<TriageEntry[]>>()
const actOn = vi.fn<(key: string, action: string, comment?: string) => Promise<unknown>>()
vi.mock('@/api/work', async (orig) => ({
  ...(await orig<typeof import('@/api/work')>()),
  listTriage: (q: { project?: string }, ws?: string) => listTriage(q, ws),
  actOn: (key: string, action: string, comment?: string) => actOn(key, action, comment),
}))
vi.mock('@/components/work/useDirectory', () => ({
  useDirectory: () => ({
    people: [],
    agents: [],
    labels: [],
    projects: [{ id: 1, name: 'Board deck', address: 'connect/board-deck', handle: 'board-deck' }],
  }),
}))
vi.mock('@/components/work/TaskDetailView', () => ({
  TaskDetailView: ({ taskKey }: { taskKey: string }) => <div>Detail of {taskKey}</div>,
}))

const { TriagePage } = await import('./TriagePage')

const entry = (over: Partial<TriageEntry>): TriageEntry => ({
  kind: 'task', ref: 'CON-1', title: 'Send the EOI', why: 'They asked twice', proposed_by: 'eva', workspace: 'connect',
  project_address: null, project_name: null, url: '/w/connect/tasks/CON-1', created_at: '2026-10-01T10:00:00Z',
  backers: 2, passes: 0, ask_kind: 'review', confidence: 'high', priority: 2, human_why: [], ...over,
})

const ROWS = [
  entry({}),
  entry({ ref: 'CON-2', title: 'Draft the budget', created_at: '2026-10-02T10:00:00Z' }),
  entry({ kind: 'project', ref: 'connect/kenya', title: 'Kenya pilot', url: '/w/connect/projects/kenya', created_at: '2026-10-03T10:00:00Z' }),
]

function Where() {
  const l = useLocation()
  return <div data-testid="where">{l.pathname + l.search}</div>
}

async function show(rows = ROWS) {
  listTriage.mockResolvedValue(rows)
  render(
    <MemoryRouter initialEntries={['/w/connect/triage']}>
      <Routes>
        <Route path="/w/:workspace/triage" element={<><TriagePage /><Where /></>} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  )
  await screen.findByText('Send the EOI')
}

const press = (key: string, target: Element | Window = window) => act(() => {
  fireEvent.keyDown(target, { key })
})
const rowRefs = () => Array.from(document.querySelectorAll('[data-triage]')).map((r) => r.getAttribute('data-triage'))
const focusedRef = () => document.querySelector('[data-triage][aria-current="true"]')?.getAttribute('data-triage')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.restoreAllMocks()
})

describe('TriagePage', () => {
  it('lists every item oldest first, reads the workspace, and focuses the first', async () => {
    await show([ROWS[1], ROWS[0]])
    expect(listTriage).toHaveBeenCalledWith({ project: '' }, 'connect')
    expect(rowRefs()).toEqual(['CON-1', 'CON-2'])
    expect(focusedRef()).toBe('CON-1')
    expect(screen.getAllByText('They asked twice').length).toBeGreaterThan(0)
  })

  it.each([
    ['a', 'accept'],
    ['s', 'approve'],
    ['b', 'backlog'],
    ['d', 'decline'],
    ['h', 'snooze'],
  ])('%s acts on the focused row and takes it off the list', async (key, action) => {
    actOn.mockResolvedValue({})
    await show()
    press('j')
    expect(focusedRef()).toBe('CON-2')
    press(key)
    await waitFor(() => expect(actOn).toHaveBeenCalledWith('CON-2', action, ''))
    expect(rowRefs()).toEqual(['CON-1', 'connect/kenya'])
  })

  it('puts a refused row back, with the server’s reason', async () => {
    actOn.mockRejectedValue(new Error('Only an editor can decide this'))
    await show()
    press('a')
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain('CON-1: Only an editor can decide this')
    expect(rowRefs()).toEqual(['CON-1', 'CON-2', 'connect/kenya'])
  })

  it('says Start now starts the proposing agent', async () => {
    await show()
    expect(screen.getByRole('button', { name: 'Start CON-1' }).getAttribute('title')).toContain('starts Eva now')
  })

  it('M asks for the key it duplicates', async () => {
    actOn.mockResolvedValue({})
    vi.spyOn(window, 'prompt').mockReturnValue('con-9')
    await show()
    press('m')
    await waitFor(() => expect(actOn).toHaveBeenCalledWith('CON-1', 'duplicate', 'CON-9'))
  })

  it('the buttons decide too', async () => {
    actOn.mockResolvedValue({})
    await show()
    fireEvent.click(screen.getByRole('button', { name: 'Backlog CON-2' }))
    await waitFor(() => expect(actOn).toHaveBeenCalledWith('CON-2', 'backlog', ''))
  })

  it('Space peeks the focused task; Enter on a project opens its page', async () => {
    await show()
    press(' ')
    // The peek mounts through a URL change: wait for it rather than racing a busy runner.
    expect((await screen.findByTestId('task-peek')).textContent).toContain('Detail of CON-1')
    press('j')
    press('j')
    await waitFor(() => expect(focusedRef()).toBe('connect/kenya'))
    press('a') // a project is decided on its own page
    expect(actOn).not.toHaveBeenCalled()
    press('Enter')
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/w/connect/projects/kenya'))
  })

  it('ignores its keys while you type in a box', async () => {
    await show()
    const select = screen.getByLabelText('Filter by project')
    press('a', select)
    press('j', select)
    expect(actOn).not.toHaveBeenCalled()
    expect(focusedRef()).toBe('CON-1')
  })

  it('filters by project', async () => {
    await show()
    fireEvent.change(screen.getByLabelText('Filter by project'), { target: { value: 'connect/board-deck' } })
    await waitFor(() => expect(listTriage).toHaveBeenLastCalledWith({ project: 'connect/board-deck' }, 'connect'))
  })

  it('says when nothing is waiting', async () => {
    listTriage.mockResolvedValue([])
    render(
      <MemoryRouter initialEntries={['/w/connect/triage']}>
        <Routes>
          <Route path="/w/:workspace/triage" element={<TriagePage />} />
        </Routes>
      </MemoryRouter>,
    )
    expect((await screen.findByTestId('triage-empty')).textContent).toContain('Nothing is waiting for a decision')
  })
})
