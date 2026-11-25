// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

const get = vi.fn()
const decide = vi.fn()
const withPasskey = vi.fn()
vi.mock('@/api/approvals', () => ({
  getApproval: (k: string) => get(k),
  decideApproval: (id: number, d: string, c: string) => decide(id, d, c),
  approveWithPasskey: (id: number, c: string, choice?: string) => withPasskey(id, c, choice),
}))
const here = { passkeys: true }
const opened = vi.fn()
vi.mock('@/auth/passkeyHere', () => ({
  PASSKEY_IN_BROWSER: 'Passkeys work in your browser, not in this app.',
  passkeysWorkHere: () => here.passkeys,
  openInBrowser: (t: string) => opened(t),
  useWhileAway: () => undefined,
}))

import { ApprovalPage } from './ApprovalPage'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  here.passkeys = true
})

const CARD = {
  id: 7, key: 'APR-7', kind: 'workspace.member.add', kind_label: 'Add someone to a workspace',
  needs: 'an admin or owner of the workspace', title: 'Add wouter@dimagi.com to Connect as editor',
  summary: 'wouter@dimagi.com would become editor of Connect (connect).', status: 'pending', workspace: 'connect',
  url: '/w/connect/approvals/7', page_url: 'https://canopy.example/w/connect/approvals/7', step_up: false,
  requested_by: 'Trellis', reason: 'He asked in #canopy-support',
  evidence: [{ quote: 'can you add me to connect?', who: 'Wouter', source_url: 'https://slack.example/p1' }],
  plan: ['wouter@dimagi.com becomes editor of Connect', 'If they already have an account they are added now'],
  if_declined: 'Nothing happens. The request is closed.', source_url: '', created_at: '2026-10-30T10:00:00Z',
  expires_at: null, sent_to: ['Jonathan Jackson (push, slack)'], decided: null, outcome: 'Waiting for a decision.',
  result: {}, error: '', can_decide: true, choices: [], history: [{ kind: 'created', by: '', at: '2026-10-30T10:00:00Z' }],
}

function open(path = '/w/connect/approvals/7', route = '/w/:workspace/approvals/:approvalId') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path={route} element={<ApprovalPage />} />
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

  it("links this Canopy's own address in a plan, and never one a submitter wrote", async () => {
    const review = `${window.location.origin}/w/connect/apps/personal/review/9`
    get.mockResolvedValue({
      ...CARD,
      plan: ['Says it sends: text to https://evil.example/page', `Read the code and the changes: ${review}`],
    })
    open()
    const plan = await screen.findByTestId('approval-plan')
    expect(plan.textContent).toContain('https://evil.example/page')
    expect(screen.queryByRole('link', { name: 'https://evil.example/page' })).toBeNull()
    expect(screen.getByRole('link', { name: review }).getAttribute('href')).toBe(review)
  })

  it('says a passkey kind needs the passkey to approve and not to decline', async () => {
    get.mockResolvedValue({ ...CARD, step_up: true })
    open()
    expect((await screen.findByTestId('approval-approve')).textContent).toBe('Approve with your passkey')
    expect(screen.getByTestId('approval-passkey').textContent).toContain('needs your passkey')
    expect(screen.getByTestId('approval-decline').textContent).toBe('Decline')
  })

  it('offers a plain Approve for a kind that needs no passkey', async () => {
    get.mockResolvedValue(CARD)
    open()
    expect((await screen.findByTestId('approval-approve')).textContent).toBe('Approve')
    expect(screen.queryByTestId('approval-passkey')).toBeNull()
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

  it('approves a passkey kind with a touch made for this request, and declines it without one', async () => {
    get.mockResolvedValue({ ...CARD, step_up: true })
    withPasskey.mockResolvedValue({ ...CARD, step_up: true, status: 'done', can_decide: false,
      outcome: 'Approved by you.', history: [{ kind: 'executed', by: 'you', at: null }] })
    open()
    fireEvent.click(await screen.findByTestId('approval-approve'))
    await waitFor(() => expect(screen.getByTestId('approval-outcome').textContent).toContain('Approved and done'))
    expect(withPasskey).toHaveBeenCalledWith(7, '', '')
    expect(decide).not.toHaveBeenCalled()
  })

  it('declines a passkey kind with no passkey', async () => {
    get.mockResolvedValue({ ...CARD, step_up: true })
    decide.mockResolvedValue({ ...CARD, step_up: true, status: 'declined', can_decide: false })
    open()
    fireEvent.click(await screen.findByTestId('approval-decline'))
    await waitFor(() => expect(decide).toHaveBeenCalledWith(7, 'decline', ''))
    expect(withPasskey).not.toHaveBeenCalled()
  })

  it('in the Runner app, where no passkey works, sends a passkey kind to the browser to approve', async () => {
    here.passkeys = false
    get.mockResolvedValue({ ...CARD, step_up: true })
    open()
    fireEvent.click(await screen.findByTestId('approval-approve'))
    expect(opened).toHaveBeenCalledWith('/w/connect/approvals/7')
    expect(screen.getByTestId('approval-in-browser').textContent).toContain('Waiting for you to approve')
    expect(withPasskey).not.toHaveBeenCalled()
    expect(decide).not.toHaveBeenCalled()
  })

  it('offers a personal kind\'s choices, read-only first, and approves the one picked with a passkey', async () => {
    const choices = [
      { value: 'read', label: 'Allow read-only (30 min)' },
      { value: 'write', label: 'Allow read + write (30 min)' },
    ]
    get.mockResolvedValue({ ...CARD, kind: 'agent.act_as', step_up: true, choices, url: '/approvals/7' })
    withPasskey.mockResolvedValue({ ...CARD, status: 'done', can_decide: false, choices: [] })
    open('/approvals/7', '/approvals/:approvalId')
    expect((await screen.findByTestId('approval-choose-read')).textContent).toBe('Allow read-only (30 min)')
    expect(screen.queryByTestId('approval-approve')).toBeNull()
    fireEvent.click(screen.getByTestId('approval-choose-write'))
    await waitFor(() => expect(withPasskey).toHaveBeenCalledWith(7, '', 'write'))
  })
})
