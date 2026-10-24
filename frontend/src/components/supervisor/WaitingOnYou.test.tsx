// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const listMyTasks = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  listMyTasks: (...a: unknown[]) => listMyTasks(...a),
}))
const updateTask = vi.fn()
vi.mock('@/api/work', async (orig) => ({
  ...(await orig<typeof import('@/api/work')>()),
  updateTask: (...a: unknown[]) => updateTask(...a),
}))
vi.mock('@/components/work/useDirectory', () => ({
  useDirectory: () => ({ people: [], agents: [], labels: [], projects: [] }),
}))
vi.mock('@/components/work/TaskDetailView', () => ({
  TaskDetailView: ({ taskKey }: { taskKey: string }) => <div>Detail of {taskKey}</div>,
}))
vi.mock('@/workspace/WorkspaceProvider', () => ({
  useActiveWorkspace: () => 'connect',
  useWorkspaceList: () => [{ slug: 'connect', display_name: 'Connect', role: 'owner' }],
}))
vi.mock('@/auth/AuthProvider', () => ({
  useAuth: () => ({ status: 'authenticated', user: { name: 'Jonathan', email: 'jj@dimagi.com' } }),
}))

const { WaitingOnYou, loadWaitingOnYou } = await import('./WaitingOnYou')

afterEach(() => {
  cleanup()
  listMyTasks.mockReset()
  updateTask.mockReset()
})

function task(over: object = {}) {
  return {
    key: 'CON-4',
    workspace: 'connect',
    agent_slug: 'eva',
    ext_id: 'T4',
    title: 'Send the board deck to Neal',
    next_action: '',
    status: 'suggested',
    priority: 0,
    labels: [],
    owner: '',
    assigned: '',
    ask_kind: 'review',
    ask_body: 'Ok to send?',
    ask_open: true,
    ask_closed_at: null,
    on_approve: [],
    batch_key: '',
    origin: 'api',
    confidence: '',
    score: '',
    review: '',
    rationale: '',
    source_url: '',
    plan: '',
    links: [],
    notes: '',
    position: 0,
    created_at: '2026-10-05T12:00:00Z',
    updated_at: '2026-10-05T12:00:00Z',
    ...over,
  } as never
}

const show = (ui: React.ReactElement) => render(<MemoryRouter initialEntries={['/supervisor?tab=waiting']}>{ui}</MemoryRouter>)

describe('WaitingOnYou', () => {
  it('re-reads when canopy says a task moved', async () => {
    const { resourceChanged } = await import('@/widget/pageInvalidation')
    const onChanged = vi.fn()
    show(<WaitingOnYou tasks={[]} canEdit={() => false} onChanged={onChanged} />)
    expect(resourceChanged('task://')).toBe(1)
    expect(onChanged).toHaveBeenCalledTimes(1)
  })

  it('loads your waiting-on-me inbox across every workspace', async () => {
    listMyTasks.mockResolvedValue([])
    await loadWaitingOnYou()
    expect(listMyTasks).toHaveBeenCalledWith({ waiting: 'me' })
  })

  it('renders the waiting tasks as the shared task table, with who has each', () => {
    show(<WaitingOnYou tasks={[task()]} canEdit={() => false} onChanged={() => {}} />)
    expect(screen.getByTestId('task-table')).toBeTruthy()
    const row = document.querySelector('[data-task="CON-4"]')
    expect(row?.textContent).toContain('Send the board deck to Neal')
    expect(row?.textContent).toContain('Eva')
    expect(row?.textContent).toContain('Decide')
  })

  it('opens a task beside the list when its title is clicked', () => {
    show(<WaitingOnYou tasks={[task()]} canEdit={() => false} onChanged={() => {}} />)
    expect(screen.queryByTestId('task-peek')).toBeNull()
    fireEvent.click(screen.getByText('Send the board deck to Neal'))
    expect(screen.getByTestId('task-peek').textContent).toContain('Detail of CON-4')
  })

  it('ranks reviews before questions before parked tasks', () => {
    show(
      <WaitingOnYou
        tasks={[
          task({ key: 'CON-1', ask_kind: '', ask_open: false, status: 'in_progress' }),
          task({ key: 'CON-2', ask_kind: 'question' }),
          task({ key: 'CON-3', ask_kind: 'review' }),
        ]}
        canEdit={() => false}
        onChanged={() => {}}
      />,
    )
    const order = Array.from(document.querySelectorAll('[data-task]')).map((el) => el.getAttribute('data-task'))
    expect(order).toEqual(['CON-3', 'CON-2', 'CON-1'])
  })

  it('says so when nothing is waiting', () => {
    show(<WaitingOnYou tasks={[]} canEdit={() => false} onChanged={() => {}} />)
    expect(screen.getByTestId('waiting-empty').textContent).toBe('Nothing waiting on you.')
  })
})
