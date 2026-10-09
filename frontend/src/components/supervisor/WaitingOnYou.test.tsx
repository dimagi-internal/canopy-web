// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

const listFleetTasks = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  listFleetTasks: (...a: unknown[]) => listFleetTasks(...a),
}))

const { WaitingOnYou, loadWaitingOnYou } = await import('./WaitingOnYou')

afterEach(() => {
  cleanup()
  listFleetTasks.mockReset()
})

function task(over: object = {}) {
  return {
    agent_slug: 'eva',
    ext_id: 'T4',
    title: 'Send the board deck to Neal',
    next_action: '',
    status: 'suggested',
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

describe('WaitingOnYou', () => {
  it('re-reads when canopy says a task moved', async () => {
    const { resourceChanged } = await import('@/widget/pageInvalidation')
    const onChanged = vi.fn()
    render(<WaitingOnYou tasks={[]} canEdit={() => false} onChanged={onChanged} />)
    expect(resourceChanged('task://')).toBe(1)
    expect(onChanged).toHaveBeenCalledTimes(1)
  })

  it('loads the fleet waiting-on-me filter', async () => {
    listFleetTasks.mockResolvedValue([])
    await loadWaitingOnYou()
    expect(listFleetTasks).toHaveBeenCalledWith({ waiting: 'me' })
  })

  it('renders a fleet task with its agent tag and an Approve button', () => {
    render(<WaitingOnYou tasks={[task()]} canEdit={() => false} onChanged={() => {}} />)
    const card = screen.getByTestId('task-T4')
    expect(card.textContent).toContain('eva')
    expect(screen.getByRole('button', { name: /^Approve/ })).toBeTruthy()
  })

  it('ranks reviews before questions before parked tasks', () => {
    render(
      <WaitingOnYou
        tasks={[
          task({ ext_id: 'T1', ask_kind: '', ask_open: false, status: 'in_progress' }),
          task({ ext_id: 'T2', ask_kind: 'question' }),
          task({ ext_id: 'T3', ask_kind: 'review' }),
        ]}
        canEdit={() => false}
        onChanged={() => {}}
      />,
    )
    const order = screen.getAllByTestId(/^task-T\d$/).map((el) => el.getAttribute('data-testid'))
    expect(order).toEqual(['task-T3', 'task-T2', 'task-T1'])
  })

  it('says so when nothing is waiting', () => {
    render(<WaitingOnYou tasks={[]} canEdit={() => false} onChanged={() => {}} />)
    expect(screen.getByTestId('waiting-empty').textContent).toBe('Nothing waiting on you.')
  })
})
