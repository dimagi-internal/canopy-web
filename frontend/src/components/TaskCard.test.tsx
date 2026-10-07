// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const actOnTask = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  actOnTask: (...a: unknown[]) => actOnTask(...a),
}))

const { availableActions, TaskCard } = await import('./TasksBoard')
const { AgentApiError } = await import('@/api/agents')

afterEach(() => {
  cleanup()
  actOnTask.mockReset()
})

const t = (o: object) => ({ status: 'suggested', ask_kind: '', ask_open: false, on_approve: [], ...o }) as never

describe('availableActions', () => {
  it('open review', () => expect(availableActions(t({ ask_kind: 'review', ask_open: true }), false)).toEqual(['approve', 'decline']))
  it('open question', () => expect(availableActions(t({ ask_kind: 'question', ask_open: true }), false)).toEqual(['reply', 'decline']))
  it('live task, editor', () => expect(availableActions(t({ status: 'in_progress' }), true)).toEqual(['reply', 'dispatch', 'done']))
  it('live task, viewer', () => expect(availableActions(t({ status: 'suggested' }), false)).toEqual(['reply']))
  it('open review, editor', () =>
    expect(availableActions(t({ ask_kind: 'review', ask_open: true }), true)).toEqual(['approve', 'decline', 'dispatch', 'done']))
  it('finished', () => expect(availableActions(t({ status: 'done' }), true)).toEqual([]))
  it('declined', () => expect(availableActions(t({ status: 'declined' }), true)).toEqual([]))
})

function task(over: object = {}) {
  return {
    agent_slug: 'eva',
    ext_id: 'T2',
    title: 'Pick a day for the board meeting',
    next_action: '',
    status: 'in_progress',
    owner: '',
    assigned: '',
    ask_kind: 'question',
    ask_body: 'Which **day** works?',
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

const buttons = () => screen.queryAllByRole('button').map((b) => b.textContent)

describe('TaskCard', () => {
  it('answers a question with the typed reply', async () => {
    actOnTask.mockResolvedValue({ task: {}, action: {}, turn_ids: [] })
    const onChanged = vi.fn()
    render(<TaskCard task={task()} onChanged={onChanged} canEdit={false} />)
    fireEvent.change(screen.getByTestId('task-reply-T2'), { target: { value: 'Tuesday' } })
    fireEvent.click(screen.getByRole('button', { name: 'Answer' }))
    await waitFor(() => expect(actOnTask).toHaveBeenCalledWith('eva', 'T2', 'reply', 'Tuesday'))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
  })

  it('shows the ask body and the card age', () => {
    render(<TaskCard task={task()} canEdit={false} />)
    expect(screen.getByTestId('ask-T2').textContent).toContain('day')
    expect(screen.getByTestId('task-age')).toBeTruthy()
  })

  it('labels a question with a follow-up "Answer & run"', () => {
    render(<TaskCard task={task({ on_approve: [{ prompt: 'go' }] })} canEdit={false} />)
    expect(buttons()).toContain('Answer & run')
  })

  it('offers Approve & run / Decline on an open review with a follow-up, and no reply box', () => {
    render(<TaskCard task={task({ ask_kind: 'review', on_approve: [{ prompt: 'go' }] })} canEdit={false} />)
    expect(buttons()).toEqual(['Approve & run', 'Decline'])
    expect(screen.queryByTestId('task-reply-T2')).toBeNull()
  })

  it('a live task offers Reply, and editors also get Dispatch and Done', () => {
    render(<TaskCard task={task({ ask_kind: '', ask_open: false })} canEdit />)
    expect(buttons()).toEqual(['Reply', 'Eva, do this now', 'Mark done'])
  })

  it('a viewer gets no Dispatch / Done', () => {
    render(<TaskCard task={task({ ask_kind: '', ask_open: false })} canEdit={false} />)
    expect(buttons()).toEqual(['Reply'])
  })

  it('a finished task offers nothing', () => {
    render(<TaskCard task={task({ status: 'done', ask_open: false })} canEdit />)
    expect(buttons()).toEqual([])
  })

  it('approve posts the approve action', async () => {
    actOnTask.mockResolvedValue({ task: {}, action: {}, turn_ids: [] })
    render(<TaskCard task={task({ ask_kind: 'review' })} canEdit={false} />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(actOnTask).toHaveBeenCalledWith('eva', 'T2', 'approve', undefined))
  })

  it('a 409 (ask already closed) renders inline and refetches', async () => {
    actOnTask.mockRejectedValue(new AgentApiError('actOnTask failed', 409, 'This ask is already closed.'))
    const onChanged = vi.fn()
    render(<TaskCard task={task({ ask_kind: 'review' })} onChanged={onChanged} canEdit={false} />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(screen.getByText('This ask is already closed.')).toBeTruthy())
    expect(screen.getByText('This ask is already closed.').className).toContain('text-destructive')
    expect(onChanged).toHaveBeenCalled()
  })

  it('a 422 renders inline without refetching', async () => {
    actOnTask.mockRejectedValue(new AgentApiError('actOnTask failed', 422, 'A reply needs text.'))
    const onChanged = vi.fn()
    render(<TaskCard task={task()} onChanged={onChanged} canEdit={false} />)
    fireEvent.change(screen.getByTestId('task-reply-T2'), { target: { value: ' x ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Answer' }))
    await waitFor(() => expect(screen.getByText('A reply needs text.')).toBeTruthy())
    expect(onChanged).not.toHaveBeenCalled()
  })

  it('tags the agent when asked to', () => {
    render(<TaskCard task={task()} canEdit={false} showAgent />)
    expect(screen.getByTestId('task-T2').textContent).toContain('eva')
  })
})
