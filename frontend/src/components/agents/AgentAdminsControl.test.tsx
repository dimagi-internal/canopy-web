// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const listAgentAdmins = vi.fn()
const grantAgentAdmin = vi.fn()
const revokeAgentAdmin = vi.fn()
const listMembers = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  listAgentAdmins: (...a: unknown[]) => listAgentAdmins(...a),
  grantAgentAdmin: (...a: unknown[]) => grantAgentAdmin(...a),
  revokeAgentAdmin: (...a: unknown[]) => revokeAgentAdmin(...a),
}))
vi.mock('@/api/workspaces', async (orig) => ({
  ...(await orig<typeof import('@/api/workspaces')>()),
  listMembers: (...a: unknown[]) => listMembers(...a),
}))

const { AgentAdminsControl } = await import('./AgentAdminsControl')

const OWNER = { user_id: 1, name: 'Op', email: 'op@dimagi.com', is_owner: true, granted_by_email: null, granted_at: null }
const ED = { user_id: 3, name: 'ed@dimagi.com', email: 'ed@dimagi.com', is_owner: false,
  granted_by_email: 'op@dimagi.com', granted_at: '2026-09-21T00:00:00Z' }
const members = [
  { user_id: 1, email: 'op@dimagi.com', role: 'editor', joined_at: '2026-01-01T00:00:00Z' },
  { user_id: 2, email: 'boss@dimagi.com', role: 'owner', joined_at: '2026-01-01T00:00:00Z' },
  { user_id: 3, email: 'ed@dimagi.com', role: 'editor', joined_at: '2026-01-01T00:00:00Z' },
]

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentAdminsControl', () => {
  it('lists who holds the keys and offers nothing to someone who may not manage them', async () => {
    listAgentAdmins.mockResolvedValue([OWNER, ED])
    render(<AgentAdminsControl agentSlug="ace" workspace="connect" canManage={false} />)
    const rows = await screen.findAllByTestId('agent-admin')
    expect(rows.map((r) => r.textContent)).toEqual([
      'Op (op@dimagi.com)Owner', 'ed@dimagi.comGranted by op@dimagi.com',
    ])
    expect(screen.queryByRole('button', { name: 'Add admin' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Revoke' })).toBeNull()
  })

  it('grants from members who are not already admins and are not workspace owners', async () => {
    listAgentAdmins.mockResolvedValue([OWNER])
    listMembers.mockResolvedValue(members)
    grantAgentAdmin.mockResolvedValue([OWNER, ED])
    render(<AgentAdminsControl agentSlug="ace" workspace="connect" canManage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add admin' }))
    const select = await screen.findByLabelText('New admin')
    // The owner already is one; a workspace owner is one implicitly.
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual([
      'Choose a member…', 'ed@dimagi.com (editor)',
    ])
    fireEvent.change(select, { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: 'Grant' }))
    await waitFor(() => expect(grantAgentAdmin).toHaveBeenCalledWith('ace', 3))
    expect(await screen.findAllByTestId('agent-admin')).toHaveLength(2)
  })

  it('revokes a granted admin but never offers to revoke the owner', async () => {
    listAgentAdmins.mockResolvedValue([OWNER, ED])
    revokeAgentAdmin.mockResolvedValue([OWNER])
    render(<AgentAdminsControl agentSlug="ace" workspace="connect" canManage />)
    const revoke = await screen.findAllByRole('button', { name: 'Revoke' })
    expect(revoke).toHaveLength(1)
    fireEvent.click(revoke[0])
    await waitFor(() => expect(revokeAgentAdmin).toHaveBeenCalledWith('ace', 3))
    await waitFor(() => expect(screen.getAllByTestId('agent-admin')).toHaveLength(1))
  })

  it('shows the server refusal instead of swallowing it', async () => {
    listAgentAdmins.mockResolvedValue([OWNER, ED])
    revokeAgentAdmin.mockRejectedValue(new Error('only the agent’s owner or a workspace owner can change its admins'))
    render(<AgentAdminsControl agentSlug="ace" workspace="connect" canManage />)
    fireEvent.click((await screen.findAllByRole('button', { name: 'Revoke' }))[0])
    expect((await screen.findByRole('alert')).textContent).toMatch(/only the agent/)
  })
})
