// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AgentAccessOut } from '@/api/agents'

const getAgentAccess = vi.fn()
const grantAgentAdmin = vi.fn()
const revokeAgentAdmin = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getAgentAccess: (...a: unknown[]) => getAgentAccess(...a),
  grantAgentAdmin: (...a: unknown[]) => grantAgentAdmin(...a),
  revokeAgentAdmin: (...a: unknown[]) => revokeAgentAdmin(...a),
}))

const { AgentAccessRoster } = await import('./AgentAccessRoster')

const row = (o: Partial<AgentAccessOut['members'][number]>): AgentAccessOut['members'][number] => ({
  user_id: 1, email: 'x@dimagi.com', name: 'x@dimagi.com', workspace_role: 'editor', agent_role: 'member',
  basis: 'Workspace member', granted_at: null, access: 'full', capabilities: [], full_rule: null, ...o,
})

const ACCESS: AgentAccessOut = {
  members: [
    row({ user_id: 1, email: 'op@dimagi.com', name: 'Op', agent_role: 'owner', basis: 'Owns this agent' }),
    row({ user_id: 2, email: 'boss@dimagi.com', workspace_role: 'owner', agent_role: 'admin', basis: 'Owns the workspace' }),
    row({ user_id: 3, email: 'adm@dimagi.com', agent_role: 'admin', basis: 'Made admin by op@dimagi.com' }),
    row({ user_id: 4, email: 'ed@dimagi.com', full_rule: 'member@dimagi.com:verified' }),
    row({ user_id: 5, email: 'vw@partner.org', workspace_role: 'viewer', access: 'confined', capabilities: ['ask'] }),
    row({ user_id: 6, email: 'no@partner.org', workspace_role: 'viewer', access: 'none' }),
  ],
  outsiders: [{ caller: 'contact', access: 'confined', capability: 'ask' }],
  interface_published: true,
  slack_enabled: false,
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const rowFor = (email: string) =>
  screen.getAllByTestId('agent-access-row').find((r) => r.textContent?.includes(email)) as HTMLElement

describe('AgentAccessRoster', () => {
  it('shows every person with role, reason and what they reach', async () => {
    getAgentAccess.mockResolvedValue(ACCESS)
    render(<AgentAccessRoster agentSlug="ace" canManage={false} />)
    expect(await screen.findAllByTestId('agent-access-row')).toHaveLength(6)
    expect(within(rowFor('boss@dimagi.com')).getByText('Admin')).toBeTruthy()
    expect(within(rowFor('boss@dimagi.com')).getByText('Owns the workspace')).toBeTruthy()
    expect(within(rowFor('vw@partner.org')).getByText('Only: ask')).toBeTruthy()
    expect(within(rowFor('no@partner.org')).getByText('No access')).toBeTruthy()
    expect(screen.getByText(/only ask/)).toBeTruthy()
    // no controls for someone who may not manage admins
    expect(screen.queryByText('Make admin')).toBeNull()
    expect(screen.queryByText('Remove admin')).toBeNull()
  })

  it('a manager can grant admin to a member and revoke a granted admin, never a workspace owner or the owner', async () => {
    getAgentAccess.mockResolvedValue(ACCESS)
    grantAgentAdmin.mockResolvedValue([])
    render(<AgentAccessRoster agentSlug="ace" canManage />)
    await screen.findAllByTestId('agent-access-row')

    expect(within(rowFor('op@dimagi.com')).queryByRole('button')).toBeNull()
    expect(within(rowFor('boss@dimagi.com')).queryByRole('button')).toBeNull()
    expect(screen.getByLabelText('Remove adm@dimagi.com as admin')).toBeTruthy()

    fireEvent.click(screen.getByLabelText('Make ed@dimagi.com an admin'))
    await waitFor(() => expect(grantAgentAdmin).toHaveBeenCalledWith('ace', 4))
    // the whole answer is re-read, since admin changes access as well as role
    await waitFor(() => expect(getAgentAccess).toHaveBeenCalledTimes(2))
  })

  it('says plainly when no caller rules are published', async () => {
    getAgentAccess.mockResolvedValue({ ...ACCESS, interface_published: false, outsiders: [] })
    render(<AgentAccessRoster agentSlug="ace" canManage={false} />)
    expect(await screen.findByText(/anyone who reaches this agent gets the whole agent/)).toBeTruthy()
  })
})
