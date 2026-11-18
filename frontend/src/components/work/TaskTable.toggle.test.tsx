// @vitest-environment jsdom
//
// The row's status circle is one tap: an open task is finished, a finished one is
// reopened (put back as it was when it was finished in this list). Every status is
// still a long-press, a right-click or the chevron away. A recurring template never
// completes: it shows the repeat icon and no toggle.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

vi.mock('@/auth/AuthProvider', () => ({ useAuth: () => ({ status: 'authenticated', user: { email: 'jj@dimagi.com' } }) }))
vi.mock('@/workspace/WorkspaceProvider', () => ({ useWorkspaceList: () => [{ slug: 'dim', role: 'owner' }] }))

const { TaskTable } = await import('./TaskTable')

function task(over: Record<string, unknown> = {}) {
  return {
    id: 1, key: 'DIM-1', url: '', workspace: 'dim', ext_id: 'T1', kind: 'work', title: 'Sweep the kitchen',
    next_action: '', status: 'todo', category: 'unstarted', priority: 0, labels: [], owner: '',
    assigned: '', agent_slug: null, assignee_email: null, ask_kind: '', ask_body: '', ask_open: false,
    batch_key: '', idempotency_key: '', origin: '', confidence: '', score: '', review: '', rationale: '',
    source_url: '', plan: '', due: null, notes: '', position: 0, rank: 0, drive_folder_id: '',
    drive_folder_url: '', created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z',
    ...over,
  } as never
}

const directory = { people: [], agents: [], labels: [], projects: [] }

function show(rows: unknown[], props: Record<string, unknown> = {}) {
  const onPatch = vi.fn()
  const onUndo = vi.fn()
  render(
    <MemoryRouter>
      <TaskTable tasks={rows as never} directory={directory as never} onPatch={onPatch} onUndo={onUndo} canEdit={() => true} {...props} />
    </MemoryRouter>,
  )
  return { onPatch, onUndo }
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('the status circle', () => {
  it('one tap finishes an open task', () => {
    const { onPatch } = show([task()])
    fireEvent.click(screen.getByRole('button', { name: 'Mark DIM-1 done' }))
    expect(onPatch).toHaveBeenCalledWith('DIM-1', { status: 'done' }, { status: 'done' })
  })

  it('one tap reopens a finished task: Undo when it was finished here, else todo', () => {
    const kept = show([task({ status: 'done' })], { kept: new Set(['DIM-1']) })
    fireEvent.click(screen.getByRole('button', { name: 'Reopen DIM-1' }))
    expect(kept.onUndo).toHaveBeenCalledTimes(1)
    expect(kept.onPatch).not.toHaveBeenCalled()
    cleanup()
    const plain = show([task({ status: 'done' })])
    fireEvent.click(screen.getByRole('button', { name: 'Reopen DIM-1' }))
    expect(plain.onPatch).toHaveBeenCalledWith('DIM-1', { status: 'todo' }, { status: 'todo' })
  })

  it('a long-press opens every status instead of toggling', async () => {
    vi.useFakeTimers()
    const { onPatch } = show([task()])
    const circle = screen.getByRole('button', { name: 'Mark DIM-1 done' })
    fireEvent.pointerDown(circle)
    await act(async () => {
      vi.advanceTimersByTime(600)
    })
    fireEvent.pointerUp(circle)
    fireEvent.click(circle)
    expect(onPatch).not.toHaveBeenCalled()
    vi.useRealTimers()
    expect(await screen.findByRole('menu')).toBeTruthy()
  })

  it('a right-click opens every status too', async () => {
    const { onPatch } = show([task()])
    fireEvent.contextMenu(screen.getByRole('button', { name: 'Mark DIM-1 done' }))
    expect(await screen.findByRole('menu')).toBeTruthy()
    expect(onPatch).not.toHaveBeenCalled()
  })

  it('a repeating template cannot be completed from its circle', () => {
    const { onPatch } = show([task({ key: 'DIM-5', status: 'backlog', recurrence: { every: 'week', weekday: 'wed', auto: true } })])
    expect(screen.queryByRole('button', { name: /Mark DIM-5 done/ })).toBeNull()
    expect(screen.getByTestId('repeats-DIM-5').textContent).toContain('every Wednesday')
    expect(screen.queryByRole('button', { name: /^Status/ })).toBeNull()
    expect(onPatch).not.toHaveBeenCalled()
  })

  it('a reader sees the status and no toggle', () => {
    show([task()], { canEdit: () => false })
    expect(screen.queryByRole('button', { name: 'Mark DIM-1 done' })).toBeNull()
  })
})
