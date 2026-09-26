// @vitest-environment jsdom
//
// An agent is linked to the canopy USER it is, picked from real workspace
// members — not an address typed into a box. A refusal shows the server's
// reason, which is how you learn another instance already holds that user.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { linkAgentCanopyUser, listMembers } = vi.hoisted(() => ({
  linkAgentCanopyUser: vi.fn(),
  listMembers: vi.fn(),
}))
vi.mock('@/api/agents', () => ({ linkAgentCanopyUser }))
vi.mock('@/api/workspaces', () => ({ listMembers }))

const { AgentCanopyUserControl } = await import('./AgentCanopyUserControl')

const MEMBERS = [
  { user_id: 1, email: 'jjackson@dimagi.com', role: 'owner' },
  { user_id: 9, email: 'echo@dimagi-ai.com', role: 'editor' },
]

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentCanopyUserControl', () => {
  it('says when an agent is not linked, and offers nothing to a non-admin', () => {
    render(<AgentCanopyUserControl agentSlug="echo" workspace="connect" initialUser={null} canEdit={false} />)
    expect(screen.getByTestId('agent-canopy-user').textContent).toBe('Not linked')
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('links a member picked from the workspace', async () => {
    listMembers.mockResolvedValue(MEMBERS)
    linkAgentCanopyUser.mockResolvedValue({ canopy_user: { user_id: 9, name: 'Echo', email: 'echo@dimagi-ai.com' } })
    render(<AgentCanopyUserControl agentSlug="echo" workspace="connect" initialUser={null} canEdit />)
    fireEvent.click(screen.getByRole('button', { name: 'Link canopy user' }))
    await waitFor(() => expect(screen.getByLabelText('Canopy user')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Canopy user'), { target: { value: '9' } })
    fireEvent.click(screen.getByRole('button', { name: 'Link' }))
    await waitFor(() => expect(screen.getByTestId('agent-canopy-user').textContent).toBe('echo@dimagi-ai.com'))
    expect(listMembers).toHaveBeenCalledWith('connect')
    expect(linkAgentCanopyUser).toHaveBeenCalledWith('echo', 9)
  })

  it('shows why a link was refused', async () => {
    listMembers.mockResolvedValue(MEMBERS)
    linkAgentCanopyUser.mockRejectedValue(new Error("echo@dimagi-ai.com is already the canopy user of 'echo'"))
    render(<AgentCanopyUserControl agentSlug="echo-2" workspace="connect" initialUser={null} canEdit />)
    fireEvent.click(screen.getByRole('button', { name: 'Link canopy user' }))
    await waitFor(() => expect(screen.getByLabelText('Canopy user')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Canopy user'), { target: { value: '9' } })
    fireEvent.click(screen.getByRole('button', { name: 'Link' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain("already the canopy user of 'echo'"))
  })
})
