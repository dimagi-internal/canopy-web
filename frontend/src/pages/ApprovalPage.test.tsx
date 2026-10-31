// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

const get = vi.fn()
const decide = vi.fn()
vi.mock('@/api/approvals', () => ({
  getApproval: (k: string) => get(k),
  decideApproval: (id: number, d: string, c: string) => decide(id, d, c),
}))

import { ApprovalPage } from './ApprovalPage'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const CARD = {
  id: 7, key: 'APR-7', kind: 'workspace.member.add', kind_label: 'Add someone to a workspace',
  needs: 'an admin or owner of the workspace', title: 'Add wouter@dimagi.com to Connect as editor',
  summary: 'wouter@dimagi.com would become editor of Connect (connect).', status: 'pending', workspace: 'connect',
  url: '/w/connect/approvals/7', requested_by: 'Trellis', reason: 'He asked in #canopy-support',
  evidence: [{ quote: 'can you add me to connect?', who: 'Wouter', source_url: 'https://slack.example/p1' }],
  plan: ['wouter@dimagi.com becomes editor of Connect', 'If they already have an account they are added now'],
  if_declined: 'Nothing happens. The request is closed.', source_url: '', created_at: '2026-10-30T10:00:00Z',
  expires_at: null, sent_to: ['Jonathan Jackson (push, slack)'], decided: null, outcome: 'Waiting for a decision.',
  result: {}, error: '', can_decide: true, history: [{ kind: 'created', by: '', at: '2026-10-30T10:00:00Z' }],
}

function open(path = '/w/connect/approvals/7') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/w/:workspace/approvals/:approvalId" element={<ApprovalPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ApprovalPage', () => {
  it('leads with what approving will do, and keeps the requester words apart and labelled as theirs', async () => {
    get.mockResolvedValue(CARD)
    open()
    expect(await screen.findByText('Add wouter@dimagi.com to Connect as editor')).toBeTruthy()
    expect(screen.getByTestId('approval-kind').textContent).toContain('Add someone to a workspace')
    expect(screen.getByTestId('approval-plan').textContent).toContain('wouter@dimagi.com becomes editor of Connect')
    expect(screen.getByTestId('approval-if-decline').textContent).toContain('Nothing happens')
    const theirs = screen.getByTestId('approval-theirs')
    expect(theirs.textContent).toContain('own words')
    expect(theirs.textContent).toContain('He asked in #canopy-support')
    expect(theirs.textContent).toContain('can you add me to connect?')
    expect(screen.getByTestId('approval-plan').textContent).not.toContain('He asked in')
  })

  it('approves with a note, then shows the outcome and the history', async () => {
    get.mockResolvedValue(CARD)
    decide.mockResolvedValue({ ...CARD, status: 'done', can_decide: false, outcome: 'Approved by Ada via the web and done.',
      decided: { by: 'Ada', via: 'web', at: null, comment: 'ok' },
      history: [{ kind: 'created', by: '', at: '' }, { kind: 'approved', by: 'Ada', at: '' }, { kind: 'executed', by: 'Ada', at: '' }] })
    open()
    fireEvent.change(await screen.findByLabelText('A note'), { target: { value: 'ok' } })
    fireEvent.click(screen.getByTestId('approval-approve'))
    await waitFor(() => expect(screen.getByTestId('approval-outcome').textContent).toContain('Approved and done'))
    expect(decide).toHaveBeenCalledWith(7, 'approve', 'ok')
    expect(screen.getByTestId('approval-history').textContent).toContain('executed')
    expect(screen.queryByTestId('approval-approve')).toBeNull()
  })

  it('a person without the authority can read it but not decide, and is told what it needs', async () => {
    get.mockResolvedValue({ ...CARD, can_decide: false })
    open()
    expect((await screen.findByTestId('approval-cannot')).textContent).toContain('an admin or owner of the workspace')
    expect((screen.getByTestId('approval-approve') as HTMLButtonElement).disabled).toBe(true)
  })

  it('an email link only pre-selects: it asks for one more tap and decides nothing', async () => {
    get.mockResolvedValue(CARD)
    open('/w/connect/approvals/7?do=decline')
    expect((await screen.findByTestId('approval-confirm')).textContent).toContain('decline')
    expect(decide).not.toHaveBeenCalled()
  })

  it('a failed approval shows the reason, and a lost race shows who decided first', async () => {
    get.mockResolvedValueOnce(CARD).mockResolvedValue({ ...CARD, status: 'declined', can_decide: false,
      outcome: 'Declined by Bo via Slack.', decided: { by: 'Bo', via: 'slack', at: null, comment: '' } })
    decide.mockRejectedValue(new Error('Already decided. Declined by Bo via Slack.'))
    open()
    fireEvent.click(await screen.findByTestId('approval-approve'))
    await waitFor(() => expect(screen.getByTestId('approval-outcome').textContent).toContain('Declined'))
    expect(screen.getByTestId('approval-outcome').textContent).toContain('Already decided')
  })
})
