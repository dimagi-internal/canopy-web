// @vitest-environment jsdom
//
// The proposed-project card the project page and the huddle page share: the
// decision only for whoever may make it, a line required to back with changes
// or pass, and replies as the comment thread — each the existing task action
// or backing route, pinned to the project's own workspace.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import type { ProposedProjectOut } from '@/api/agents'

const { actOnProposal, backProject, unbackProject } = vi.hoisted(() => ({
  actOnProposal: vi.fn(async () => ({})),
  backProject: vi.fn(async () => ({})),
  unbackProject: vi.fn(async () => undefined),
}))

vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  actOnProposal,
  backProject,
  unbackProject,
}))

import { ProposedProjectCard } from './ProposedProjectCard'

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
      <ProposedProjectCard proposal={p} onChanged={onChanged} />
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ProposedProjectCard', () => {
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
