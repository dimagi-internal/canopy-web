// @vitest-environment jsdom
//
// An agent's canopy login is always the account at its own email: the control links
// that account or unlinks it, and shows the server's reason when it can't.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { linkAgentCanopyUser } = vi.hoisted(() => ({ linkAgentCanopyUser: vi.fn() }))
vi.mock('@/api/agents', () => ({ linkAgentCanopyUser }))

const { AgentCanopyUserControl } = await import('./AgentCanopyUserControl')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentCanopyUserControl', () => {
  it('says when an agent is not linked, and offers nothing to a non-admin', () => {
    render(<AgentCanopyUserControl agentSlug="echo" agentEmail="echo@dimagi-ai.com" initialUser={null} canEdit={false} />)
    expect(screen.getByTestId('agent-canopy-user').textContent).toBe('Not linked')
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('links the account at its email', async () => {
    linkAgentCanopyUser.mockResolvedValue({ canopy_user: { user_id: 9, name: 'Echo', email: 'echo@dimagi-ai.com' } })
    render(<AgentCanopyUserControl agentSlug="echo" agentEmail="echo@dimagi-ai.com" initialUser={null} canEdit />)
    fireEvent.click(screen.getByRole('button', { name: 'Link echo@dimagi-ai.com' }))
    await waitFor(() => expect(screen.getByTestId('agent-canopy-user').textContent).toBe('echo@dimagi-ai.com'))
    expect(linkAgentCanopyUser).toHaveBeenCalledWith('echo', true)
  })

  it('shows a request waiting on the account’s person', async () => {
    linkAgentCanopyUser.mockResolvedValue({
      canopy_user: null,
      canopy_user_request: { user_email: 'echo@dimagi-ai.com' },
    })
    render(<AgentCanopyUserControl agentSlug="echo" agentEmail="echo@dimagi-ai.com" initialUser={null} canEdit />)
    fireEvent.click(screen.getByRole('button', { name: 'Link echo@dimagi-ai.com' }))
    await waitFor(() => expect(screen.getByTestId('agent-canopy-user-pending')).toBeTruthy())
  })

  it('shows why a link was refused', async () => {
    linkAgentCanopyUser.mockRejectedValue(new Error("echo@dimagi-ai.com is already the canopy user of 'echo'"))
    render(<AgentCanopyUserControl agentSlug="echo-2" agentEmail="echo@dimagi-ai.com" initialUser={null} canEdit />)
    fireEvent.click(screen.getByRole('button', { name: 'Link echo@dimagi-ai.com' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain("already the canopy user of 'echo'"))
  })

  it('cannot link an agent with no email', () => {
    render(<AgentCanopyUserControl agentSlug="labsdata" initialUser={null} canEdit />)
    expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(true)
  })
})
