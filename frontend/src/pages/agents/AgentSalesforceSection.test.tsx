// @vitest-environment jsdom
//
// Agents act in Salesforce only as an identity another agent LENDS them. The
// section shows whose identity this agent borrows and who borrows its own; the
// owner gets the lend box; a refusal shows the server's own reason.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import { AuthContext } from '@/auth/AuthProvider'

const { getAgentSalesforce, setAgentSalesforce, checkAgentSalesforce, deleteAgentSalesforce } = vi.hoisted(() => ({
  getAgentSalesforce: vi.fn(),
  setAgentSalesforce: vi.fn(),
  checkAgentSalesforce: vi.fn(),
  deleteAgentSalesforce: vi.fn(),
}))
vi.mock('@/api/agents', () => ({ getAgentSalesforce, setAgentSalesforce, checkAgentSalesforce, deleteAgentSalesforce }))

const { AgentSalesforceSection } = await import('./AgentSalesforceSection')

const NONE = { set: false, owner_email: 'olive@dimagi.com', lender: '', username: '', error: '', lent_to: [] }
const BORROWING = { ...NONE, set: true, lender: 'eva', username: 'eva@dimagi-ai.com', checked_at: '2026-10-08T05:03:03Z' }

function show(email: string, slug = 'echo') {
  return render(
    <AuthContext.Provider value={{ status: 'authenticated', user: { email } } as never}>
      <AgentSalesforceSection slug={slug} />
    </AuthContext.Provider>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentSalesforceSection', () => {
  it('says whose identity a borrowing agent acts as', async () => {
    getAgentSalesforce.mockResolvedValue(BORROWING)
    show('someone@dimagi.com')
    await waitFor(() => expect(screen.getByTestId('salesforce-status').textContent).toContain('eva@dimagi-ai.com'))
    expect(screen.getByTestId('salesforce-status').textContent).toContain('lent by eva')
    expect(screen.getByTestId('salesforce-status').textContent).toContain('Working')
  })

  it('shows the lender who borrows its identity', async () => {
    getAgentSalesforce.mockResolvedValue({ ...NONE, lent_to: ['echo', 'hal'] })
    show('someone@dimagi.com', 'eva')
    await waitFor(() => expect(screen.getByTestId('salesforce-lent-to').textContent).toContain('echo'))
    expect(screen.getByTestId('salesforce-lent-to').textContent).toContain('hal')
  })

  it('gives the owner a lend box and shows the server’s reason on refusal', async () => {
    getAgentSalesforce.mockResolvedValue(NONE)
    setAgentSalesforce.mockRejectedValue(new Error("you own no agent 'eve' to lend"))
    show('olive@dimagi.com')
    await waitFor(() => expect(screen.getByLabelText('Lending agent')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Lending agent'), { target: { value: 'eve' } })
    fireEvent.click(screen.getByRole('button', { name: 'Lend' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain("you own no agent 'eve'"))
    expect(setAgentSalesforce).toHaveBeenCalledWith('echo', 'eve')
  })

  it('tells anyone else it is the owner’s to lend', async () => {
    getAgentSalesforce.mockResolvedValue(NONE)
    show('someone@dimagi.com')
    await waitFor(() => expect(screen.getByTestId('agent-salesforce')).toBeTruthy())
    expect(screen.queryByTestId('salesforce-setup')).toBeNull()
    expect(screen.getByTestId('agent-salesforce').textContent).toContain('Only the agent’s owner')
  })
})
