// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const actOnTask = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  actOnTask: (...a: unknown[]) => actOnTask(...a),
}))

const { availableActions, projectHref, TaskCard, TasksBoard } = await import('./TasksBoard')
const { AgentApiError } = await import('@/api/agents')

afterEach(() => {
  cleanup()
  actOnTask.mockReset()
})

const t = (o: object) => ({ status: 'suggested', ask_kind: '', ask_open: false, on_approve: [], ...o }) as never

describe('availableActions', () => {
  it('open review', () =>
    expect(availableActions(t({ ask_kind: 'review', ask_open: true }), false)).toEqual(['reply', 'approve', 'decline']))
  it('open question', () => expect(availableActions(t({ ask_kind: 'question', ask_open: true }), false)).toEqual(['reply', 'decline']))
  it('in-progress task, editor: nudge + done', () =>
    expect(availableActions(t({ status: 'in_progress' }), true)).toEqual(['reply', 'nudge', 'done']))
  it('in-progress task, viewer', () => expect(availableActions(t({ status: 'in_progress' }), false)).toEqual(['reply']))
  it('suggested task with no ask, viewer', () =>
    expect(availableActions(t({ status: 'suggested' }), false)).toEqual(['reply', 'approve', 'decline']))
  it('suggested task whose ask is closed offers no approve/decline (the server 409s them)', () =>
    expect(availableActions(t({ status: 'suggested', ask_kind: 'question', ask_open: false }), false)).toEqual(['reply']))
  it('suggested task whose ask is closed, editor: no nudge (not in progress)', () =>
    expect(availableActions(t({ status: 'suggested', ask_kind: 'review', ask_open: false }), true)).toEqual(['reply', 'done']))
  it('open review, editor: approve starts it, so no nudge', () =>
    expect(availableActions(t({ ask_kind: 'review', ask_open: true }), true)).toEqual(['reply', 'approve', 'decline', 'done']))
  it('suggested task with no ask, editor', () =>
    expect(availableActions(t({ status: 'suggested' }), true)).toEqual(['reply', 'approve', 'decline', 'done']))
  it('in-progress task with an open review, editor', () =>
    expect(availableActions(t({ status: 'in_progress', ask_kind: 'review', ask_open: true }), true)).toEqual([
      'reply',
      'approve',
      'decline',
      'nudge',
      'done',
    ]))
  it('never offers the retired dispatch action', () => {
    for (const status of ['suggested', 'in_progress']) {
      for (const canEdit of [true, false]) {
        expect(availableActions(t({ status }), canEdit)).not.toContain('dispatch')
      }
    }
  })
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

  it('offers Reply, Approve and Decline on an open review — Approve names who starts', () => {
    render(<TaskCard task={task({ ask_kind: 'review', on_approve: [{ prompt: 'go' }] })} canEdit={false} />)
    expect(buttons()).toEqual(['Reply', 'Approve — Eva starts now', 'Decline'])
    expect(screen.getByTestId('task-reply-T2')).toBeTruthy()
  })

  it('a suggested task with no ask can be approved or declined by a viewer', () => {
    render(<TaskCard task={task({ status: 'suggested', ask_kind: '', ask_open: false })} canEdit={false} />)
    expect(buttons()).toEqual(['Reply', 'Approve — Eva starts now', 'Decline'])
  })

  it('an approve that fans out names the agent it starts', () => {
    render(<TaskCard task={task({ ask_kind: 'review', on_approve: [{ prompt: 'go', target_agent: 'hal' }] })} canEdit={false} />)
    expect(buttons()).toContain('Approve — Hal starts now')
  })

  it('approve sends the reply box text as its note', async () => {
    actOnTask.mockResolvedValue({ task: {}, action: {}, turn_ids: ['t1'] })
    render(<TaskCard task={task({ ask_kind: 'review' })} onChanged={() => {}} canEdit={false} />)
    fireEvent.change(screen.getByTestId('task-reply-T2'), { target: { value: 'one page only' } })
    fireEvent.click(screen.getByRole('button', { name: /^Approve/ }))
    await waitFor(() => expect(actOnTask).toHaveBeenLastCalledWith('eva', 'T2', 'approve', 'one page only'))
  })

  it('decline sends the reply box text as the reason, or nothing', async () => {
    actOnTask.mockResolvedValue({ task: {}, action: {}, turn_ids: [] })
    render(<TaskCard task={task({ ask_kind: 'review' })} onChanged={() => {}} canEdit={false} />)
    fireEvent.click(screen.getByRole('button', { name: 'Decline' }))
    await waitFor(() => expect(actOnTask).toHaveBeenLastCalledWith('eva', 'T2', 'decline', undefined))
    fireEvent.change(screen.getByTestId('task-reply-T2'), { target: { value: 'duplicate of T1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Decline' }))
    await waitFor(() => expect(actOnTask).toHaveBeenLastCalledWith('eva', 'T2', 'decline', 'duplicate of T1'))
  })

  it('an in-progress task offers Reply, and editors also get Nudge and Done', () => {
    render(<TaskCard task={task({ ask_kind: '', ask_open: false })} canEdit />)
    expect(buttons()).toEqual(['Reply', 'Nudge Eva', 'Mark done'])
  })

  it('nudge posts the nudge action', async () => {
    actOnTask.mockResolvedValue({ task: {}, action: {}, turn_ids: ['t1'] })
    render(<TaskCard task={task({ ask_kind: '', ask_open: false })} onChanged={() => {}} canEdit />)
    fireEvent.click(screen.getByRole('button', { name: 'Nudge Eva' }))
    await waitFor(() => expect(actOnTask).toHaveBeenCalledWith('eva', 'T2', 'nudge', undefined))
  })

  it('a viewer gets no Nudge / Done, and the reply box says their note waits', () => {
    render(<TaskCard task={task({ ask_kind: '', ask_open: false })} canEdit={false} />)
    expect(buttons()).toEqual(['Reply'])
    expect(screen.getByTestId('task-reply-T2').getAttribute('placeholder')).toBe('Leave Eva a note for its next turn…')
  })

  it('an editor’s reply box says the agent picks it up now', () => {
    render(<TaskCard task={task({ ask_kind: '', ask_open: false })} canEdit />)
    expect(screen.getByTestId('task-reply-T2').getAttribute('placeholder')).toBe('Reply — Eva picks it up now…')
  })

  it('an answer box says the agent picks it up now — for a viewer too', () => {
    render(<TaskCard task={task({ ask_kind: 'question' })} canEdit={false} />)
    expect(screen.getByTestId('task-reply-T2').getAttribute('placeholder')).toBe('Answer — Eva picks it up now…')
  })

  it('a finished task offers nothing', () => {
    render(<TaskCard task={task({ status: 'done', ask_open: false })} canEdit />)
    expect(buttons()).toEqual([])
  })

  it('approve posts the approve action', async () => {
    actOnTask.mockResolvedValue({ task: {}, action: {}, turn_ids: [] })
    render(<TaskCard task={task({ ask_kind: 'review' })} canEdit={false} />)
    fireEvent.click(screen.getByRole('button', { name: /^Approve/ }))
    await waitFor(() => expect(actOnTask).toHaveBeenCalledWith('eva', 'T2', 'approve', undefined))
  })

  it('a 409 (ask already closed) renders inline and refetches', async () => {
    actOnTask.mockRejectedValue(new AgentApiError('actOnTask failed', 409, 'This ask is already closed.'))
    const onChanged = vi.fn()
    render(<TaskCard task={task({ ask_kind: 'review' })} onChanged={onChanged} canEdit={false} />)
    fireEvent.click(screen.getByRole('button', { name: /^Approve/ }))
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

  it('replies twice on the same card — it comes back enabled after a success', async () => {
    actOnTask.mockResolvedValue({ task: {}, action: {}, turn_ids: [] })
    render(<TaskCard task={task({ ask_kind: '', ask_open: false })} onChanged={() => {}} canEdit={false} />)
    const input = screen.getByRole('textbox', { name: 'Reply' }) as HTMLInputElement
    fireEvent.change(input, { target: { value: 'first' } })
    fireEvent.click(screen.getByRole('button', { name: 'Reply' }))
    await waitFor(() => expect(input.disabled).toBe(false))
    expect(input.value).toBe('')
    fireEvent.change(input, { target: { value: 'second' } })
    fireEvent.click(screen.getByRole('button', { name: 'Reply' }))
    await waitFor(() => expect(actOnTask).toHaveBeenCalledTimes(2))
    expect(actOnTask).toHaveBeenLastCalledWith('eva', 'T2', 'reply', 'second')
  })

  it('a double-click posts once', async () => {
    let resolve: (v: unknown) => void = () => {}
    actOnTask.mockReturnValue(new Promise((r) => (resolve = r)))
    render(<TaskCard task={task({ ask_kind: 'review' })} canEdit={false} />)
    const approve = screen.getByRole('button', { name: /^Approve/ }) as HTMLButtonElement
    fireEvent.click(approve)
    fireEvent.click(approve)
    resolve({ task: {}, action: {}, turn_ids: [] })
    await waitFor(() => expect(approve.disabled).toBe(false))
    expect(actOnTask).toHaveBeenCalledTimes(1)
  })

  it('a task assigned to its own agent is the agent working, not a human wait', () => {
    render(<TaskCard task={task({ ask_kind: '', ask_open: false, assigned: 'Eva' })} canEdit={false} />)
    expect(screen.getByTestId('task-T2').textContent).toContain('Eva')
    expect(screen.getByTestId('task-T2').textContent).not.toContain('Waiting on')
  })

  it('a task assigned to someone else waits on them', () => {
    render(<TaskCard task={task({ ask_kind: '', ask_open: false, assigned: 'echo' })} canEdit={false} />)
    expect(screen.getByTestId('task-T2').textContent).toContain('Waiting on echo')
  })

  it('names the task’s own agent, not Echo, on the working chip', () => {
    render(<TaskCard task={task({ ask_kind: '', ask_open: false, agent_slug: 'hal' })} canEdit={false} />)
    expect(screen.getByTestId('task-T2').textContent).toContain('Hal')
    expect(screen.getByTestId('task-T2').textContent).not.toContain('Echo')
  })

  it('tags the agent when asked to', () => {
    render(<TaskCard task={task()} canEdit={false} showAgent />)
    expect(screen.getByTestId('task-T2').textContent).toContain('eva')
  })
})

describe('TaskCard project', () => {
  const inProject = { project_ext_id: 'P2', project_name: 'Connect Enterprise' }

  it('links the task to its project page in the route’s workspace', () => {
    render(
      <MemoryRouter initialEntries={['/w/connect/agents/eva/tasks']}>
        <Routes>
          <Route
            path="/w/:workspace/agents/:slug/tasks"
            element={<TaskCard task={task(inProject)} canEdit={false} />}
          />
        </Routes>
      </MemoryRouter>,
    )
    const link = screen.getByTestId('task-project-T2')
    expect(link.tagName).toBe('A')
    expect(link.getAttribute('href')).toBe('/w/connect/agents/eva/projects/P2')
    expect(link.textContent).toContain('P2')
    expect(link.textContent).toContain('Connect Enterprise')
  })

  it('uses the workspace it is given off a workspace route (the fleet queue)', () => {
    render(
      <MemoryRouter initialEntries={['/supervisor']}>
        <TaskCard task={task(inProject)} canEdit={false} workspace="dimagi" />
      </MemoryRouter>,
    )
    expect(screen.getByTestId('task-project-T2').getAttribute('href')).toBe(
      '/w/dimagi/agents/eva/projects/P2',
    )
  })

  it('shows a muted "No project" pill, not a link, when the task has none', () => {
    render(<TaskCard task={task({ project_ext_id: null })} canEdit={false} />)
    const pill = screen.getByTestId('task-project-T2')
    expect(pill.tagName).toBe('SPAN')
    expect(pill.textContent).toBe('No project')
  })

  it('hides the project when the caller says every card is in one', () => {
    render(<TaskCard task={task(inProject)} canEdit={false} showProject={false} />)
    expect(screen.queryByTestId('task-project-T2')).toBeNull()
  })

  it('without a workspace, the link resolves through /agents/* (the active workspace)', () =>
    expect(projectHref(task(inProject))).toBe('/agents/eva/projects/P2'))
})

describe('TasksBoard sections', () => {
  it('on eva’s board a task assigned eva is “Eva working”, not “Waiting on a human”', () => {
    render(
      <TasksBoard
        tasks={[
          task({ ext_id: 'T5', ask_kind: '', ask_open: false, assigned: 'eva' }),
          task({ ext_id: 'T6', ask_kind: '', ask_open: false, assigned: 'Jonathan' }),
        ]}
        canEdit={false}
      />,
    )
    const text = document.body.textContent ?? ''
    expect(text).toContain('Eva working')
    const waiting = screen.getByText('Waiting on a human').closest('section')!
    expect(waiting.textContent).toContain('Waiting on Jonathan')
    expect(waiting.querySelector('[data-testid="task-T5"]')).toBeNull()
  })
})
