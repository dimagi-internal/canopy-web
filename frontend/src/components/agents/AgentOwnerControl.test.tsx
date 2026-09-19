// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const transferAgentOwner = vi.fn()
const listMembers = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  transferAgentOwner: (...a: unknown[]) => transferAgentOwner(...a),
}))
vi.mock('@/api/workspaces', async (orig) => ({
  ...(await orig<typeof import('@/api/workspaces')>()),
  listMembers: (...a: unknown[]) => listMembers(...a),
}))

const { AgentOwnerControl } = await import('./AgentOwnerControl')

const members = [
  { user_id: 1, email: 'jj@dimagi.com', role: 'owner', joined_at: '2026-01-01T00:00:00Z' },
  { user_id: 2, email: 'amie@dimagi.com', role: 'editor', joined_at: '2026-01-01T00:00:00Z' },
]

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentOwnerControl', () => {
  it('shows who owns the agent and offers no control to someone who may not transfer it', () => {
    render(<AgentOwnerControl agentSlug="ace" workspace="connect" canTransfer={false}
      initialOwner={{ user_id: 1, name: 'Jonathan Jackson', email: 'jj@dimagi.com' }} />)
    expect(screen.getByTestId('agent-owner').textContent).toBe('Jonathan Jackson (jj@dimagi.com)')
    expect(screen.queryByRole('button', { name: /Transfer/ })).toBeNull()
  })

  it('assigns an owner from the workspace members', async () => {
    listMembers.mockResolvedValue(members)
    transferAgentOwner.mockResolvedValue({ owner: { user_id: 2, name: 'amie@dimagi.com', email: 'amie@dimagi.com' } })
    render(<AgentOwnerControl agentSlug="ace" workspace="connect" canTransfer initialOwner={null} />)
    expect(screen.getByTestId('agent-owner').textContent).toBe('No owner')

    fireEvent.click(screen.getByRole('button', { name: 'Assign owner' }))
    const select = await screen.findByLabelText('New owner')
    expect(listMembers).toHaveBeenCalledWith('connect')
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual([
      'Choose a member…', 'jj@dimagi.com (owner)', 'amie@dimagi.com (editor)',
    ])
    fireEvent.change(select, { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByText('amie@dimagi.com')).toBeTruthy()
    expect(transferAgentOwner).toHaveBeenCalledWith('ace', 2)
    expect(screen.queryByLabelText('New owner')).toBeNull()
  })

  it('shows the server refusal instead of pretending it worked', async () => {
    listMembers.mockResolvedValue(members)
    transferAgentOwner.mockRejectedValue(new Error('only a workspace owner or the agent\'s current owner can transfer it'))
    render(<AgentOwnerControl agentSlug="ace" workspace="connect" canTransfer
      initialOwner={{ user_id: 1, name: 'jj@dimagi.com', email: 'jj@dimagi.com' }} />)
    fireEvent.click(screen.getByRole('button', { name: 'Transfer' }))
    fireEvent.change(await screen.findByLabelText('New owner'), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect((await screen.findByRole('alert')).textContent).toMatch(/only a workspace owner/)
    expect(screen.getByTestId('agent-owner').textContent).toBe('jj@dimagi.com')
  })
})
