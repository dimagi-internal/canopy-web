// @vitest-environment jsdom
//
// The proposal card the project page and the huddle page share — for a proposed
// project and for a check-in's proposed task: the decision only for whoever may
// make it, a line required to back with changes or pass, and replies as the
// comment thread — each the existing task action or backing route, pinned to
// the proposal's own workspace.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import type { ProposedProjectOut, ProposedTaskOut } from '@/api/agents'

const { actOnProposal, backProject, unbackProject, actOnTaskProposal, backTask, unbackTask } = vi.hoisted(() => ({
  actOnProposal: vi.fn(async () => ({})),
  backProject: vi.fn(async () => ({})),
  unbackProject: vi.fn(async () => undefined),
  actOnTaskProposal: vi.fn(async () => ({})),
  backTask: vi.fn(async () => ({})),
  unbackTask: vi.fn(async () => undefined),
}))

vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  actOnProposal,
  backProject,
  unbackProject,
  actOnTaskProposal,
  backTask,
  unbackTask,
}))

import { ProposalCard } from './ProposalCard'

function proposal(over: Partial<ProposedProjectOut> = {}): ProposedProjectOut {
  return {
    id: 1, agent: 'ada', workspace: 'connect', ref: 'P1', name: 'Prompt library', status: 'proposed',
    outcome: 'Twenty tested prompts.', why: 'People rewrite the same prompts.', plan: ['Collect', 'Test'],
    url: '/w/connect/agents/ada/projects/P1', source_url: '', proposed_at: '2026-10-16T00:00:00Z',
    expires_at: '2026-10-30T00:00:00Z', owner_email: null, notes: '', decision_task: 'T4',
    decision_task_id: 4, decision_open: true, may_decide: true,
    backings: [{
      backer: { kind: 'agent', agent: 'eva', person_id: null, name: 'Eva', email: '' },
      stance: 'backs_with_changes', line: 'Start with Hindi', source: 'huddle', huddle_id: 'h1',
      stale: true, mine: false, updated_at: '2026-10-16T00:00:00Z',
    }],
    comments: [{ by: 'sophie@dimagi.com', by_agent: false, comment: 'Which groups?', at: '2026-10-16T00:00:00Z' }],
    partners: [{ agent: 'eva', ext_id: 'T9', task_id: 9, status: 'suggested', url: '/w/connect/agents/eva/tasks' }],
    ...over,
  } as ProposedProjectOut
}

function show(p: ProposedProjectOut, onChanged = () => {}) {
  return render(
    <MemoryRouter>
      <ProposalCard proposal={p} onChanged={onChanged} />
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ProposalCard — a proposed project', () => {
  it('shows what it is, who stands where, and the thread', () => {
    show(proposal())
    expect(screen.getByText('Twenty tested prompts.')).toBeTruthy()
    expect(screen.getByRole('list', { name: 'Plan' }).textContent).toContain('Collect')
    expect(screen.getByText(/Start with Hindi/)).toBeTruthy()
    expect(screen.getByText('(an earlier version)')).toBeTruthy()
    expect(screen.getByText('Which groups?')).toBeTruthy()
  })

  it('starts the project through the decision card, for someone who may decide', async () => {
    const onChanged = vi.fn()
    show(proposal(), onChanged)
    fireEvent.click(screen.getByRole('button', { name: 'Start this project' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(actOnProposal).toHaveBeenCalledWith(expect.objectContaining({ decision_task: 'T4' }), 'approve', '')
  })

  it('offers no decision to someone who may not make it', () => {
    show(proposal({ may_decide: false, owner_email: 'jj@dimagi.com' }))
    expect(screen.queryByRole('button', { name: 'Start this project' })).toBeNull()
    expect(screen.getByText(/Waiting on jj@dimagi.com/)).toBeTruthy()
  })

  it('needs a line to pass, and backs in one click', async () => {
    show(proposal())
    fireEvent.click(screen.getByRole('button', { name: 'Pass' }))
    const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Why you pass'), { target: { value: 'We have one' } })
    fireEvent.click(save)
    await waitFor(() => expect(backProject).toHaveBeenCalledWith(expect.anything(), 'passes', 'We have one'))
    // The first save is still settling (its buttons are busy): click once it lets go.
    await waitFor(() => expect((screen.getByRole('button', { name: 'Back it' }) as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: 'Back it' }))
    await waitFor(() => expect(backProject).toHaveBeenCalledWith(expect.anything(), 'backs', ''))
  })

  it('replies on the decision card', async () => {
    show(proposal({ may_decide: false }))
    fireEvent.change(screen.getByLabelText('Comment'), { target: { value: 'Count me in' } })
    fireEvent.click(screen.getByRole('button', { name: 'Reply' }))
    await waitFor(() => expect(actOnProposal).toHaveBeenCalledWith(expect.anything(), 'reply', 'Count me in'))
  })
})

function proposedTask(over: Partial<ProposedTaskOut> = {}): ProposedTaskOut {
  return {
    id: 7, agent: 'ada', workspace: 'dimagi', ref: 'T41', title: 'Write the FAQ', status: 'suggested',
    why: 'The same three questions every week.', done_when: 'The FAQ is pinned.', plan: ['Collect', 'Draft'],
    project_ref: 'P7', project_name: 'Canopy support', project_url: '/w/dimagi/agents/ada/projects/P7',
    url: '/w/dimagi/agents/ada/projects/P7', source_url: '', checkin: 1, run: 3, huddle_id: 'h1',
    proposed_at: '2026-10-20T00:00:00Z', expires_at: '2026-11-03T00:00:00Z', notes: '', decision_open: true,
    may_decide: true, backings: [], comments: [], partners: [],
    spawned: [{ agent: 'ada', ref: 'P9', name: 'FAQ bot', status: 'proposed', url: '/w/dimagi/agents/ada/projects/P9' }],
    ...over,
  } as ProposedTaskOut
}

function showTask(t: ProposedTaskOut, onChanged = () => {}) {
  return render(
    <MemoryRouter>
      <ProposalCard task={t} onChanged={onChanged} />
    </MemoryRouter>,
  )
}

describe('ProposalCard — a proposed task', () => {
  it('shows the task, its project and what it spawned', () => {
    showTask(proposedTask())
    expect(screen.getByRole('article', { name: 'Proposed task: Write the FAQ' })).toBeTruthy()
    expect(screen.getByText('The FAQ is pinned.')).toBeTruthy()
    expect(screen.getByText(/in Canopy support/)).toBeTruthy()
    expect(screen.getByRole('link', { name: 'FAQ bot' }).getAttribute('href')).toBe('/w/dimagi/agents/ada/projects/P9')
  })

  it('starts the task with its own approve', async () => {
    const onChanged = vi.fn()
    showTask(proposedTask(), onChanged)
    fireEvent.click(screen.getByRole('button', { name: 'Start this task' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(actOnTaskProposal).toHaveBeenCalledWith(expect.objectContaining({ ref: 'T41' }), 'approve', '')
    expect(actOnProposal).not.toHaveBeenCalled()
  })

  it('backs the task, not a project', async () => {
    showTask(proposedTask())
    fireEvent.click(screen.getByRole('button', { name: 'Back it' }))
    await waitFor(() =>
      expect(backTask).toHaveBeenCalledWith(expect.objectContaining({ ref: 'T41', project_ref: 'P7' }), 'backs', ''),
    )
    expect(backProject).not.toHaveBeenCalled()
  })

  it('offers no Start once it was decided', () => {
    showTask(proposedTask({ status: 'declined', decision_open: false, notes: 'Declined: we have a wiki' }))
    expect(screen.queryByRole('button', { name: 'Start this task' })).toBeNull()
    expect(screen.getByText('Declined: we have a wiki')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Back it' })).toBeNull()
  })
})
