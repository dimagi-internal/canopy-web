// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

const get = vi.fn()
const decide = vi.fn()
vi.mock('@/api/approvals', () => ({
  getApprovalCard: (k: string) => get(k),
  decideApproval: (k: string, d: string, c: string) => decide(k, d, c),
}))

import { ApprovalPage } from './ApprovalPage'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const CARD = {
  key: 'DIM-7', title: 'Add Wouter to the connect workspace', state: 'open', workspace: 'dimagi',
  url: '/w/dimagi/approvals/DIM-7', agent_slug: 'hal', agent_name: 'Hal', filed_by: 'Hal',
  summary: 'Add wouter@dimagi.com as an admin',
  evidence: [{ quote: 'can you add me as an admin on connect?', who: 'Wouter', source_url: 'https://slack.example/p1', said_at: null }],
  rationale: 'He asked in #canopy-support', plan: ['Add the membership', 'Tell Wouter'],
  will_run: [{ agent: 'hal', prompt: 'Apply approved proposal' }], if_declined: 'Nothing runs.',
  links: [], source_url: '', priority: 0, confidence: '', created_at: '2026-10-30T10:00:00Z',
  sent_to: ['Jonathan Jackson'], decided: null, can_decide: true,
}

function open(path = '/w/dimagi/approvals/DIM-7') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/w/:workspace/approvals/:taskKey" element={<ApprovalPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ApprovalPage', () => {
  it('shows everything needed to decide above the buttons', async () => {
    get.mockResolvedValue(CARD)
    open()
    expect(await screen.findByText('Add Wouter to the connect workspace')).toBeTruthy()
    expect(screen.getByTestId('approval-evidence').textContent).toContain('can you add me as an admin on connect?')
    expect(screen.getByTestId('approval-evidence').textContent).toContain('Wouter')
    expect(screen.getByTestId('approval-plan').textContent).toContain('Add the membership')
    expect(screen.getByTestId('approval-if-approve').textContent).toContain('Apply approved proposal')
    expect(screen.getByTestId('approval-if-decline').textContent).toContain('Nothing runs.')
  })

  it('approves with a note, then shows who decided', async () => {
    get.mockResolvedValue(CARD)
    decide.mockResolvedValue({ ...CARD, state: 'approved', can_decide: false,
      decided: { decision: 'approve', by: 'Jonathan Jackson', via: 'web', at: null, comment: 'go' } })
    open()
    fireEvent.change(await screen.findByLabelText('A note for the agent'), { target: { value: 'go' } })
    fireEvent.click(screen.getByTestId('approval-approve'))
    await waitFor(() => expect(screen.getByTestId('approval-outcome').textContent).toContain('Approved'))
    expect(decide).toHaveBeenCalledWith('DIM-7', 'approve', 'go')
    expect(screen.getByTestId('approval-outcome').textContent).toContain('Jonathan Jackson')
    expect(screen.queryByTestId('approval-approve')).toBeNull()
  })

  it('an email link only pre-selects: it asks for one more tap and decides nothing', async () => {
    get.mockResolvedValue(CARD)
    open('/w/dimagi/approvals/DIM-7?do=decline')
    expect((await screen.findByTestId('approval-confirm')).textContent).toContain('decline')
    expect(decide).not.toHaveBeenCalled()
  })

  it('a decided card has no buttons, and a lost race shows what happened', async () => {
    get.mockResolvedValueOnce(CARD).mockResolvedValue({ ...CARD, state: 'declined', can_decide: false,
      decided: { decision: 'decline', by: 'Bo', via: 'slack', at: null, comment: '' } })
    decide.mockRejectedValue(new Error('Already decided. Declined by Bo via Slack.'))
    open()
    fireEvent.click(await screen.findByTestId('approval-approve'))
    await waitFor(() => expect(screen.getByTestId('approval-outcome').textContent).toContain('Declined'))
    expect(screen.getByTestId('approval-outcome').textContent).toContain('Already decided')
  })
})
