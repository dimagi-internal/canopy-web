// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
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
  basis: 'Workspace member', granted_at: null, access: 'full', capabilities: [], full_rule: null, manual_only: false,
  may_request_auto: false, ...o,
})

const ACCESS: AgentAccessOut = {
  members: [
    row({ user_id: 1, email: 'op@dimagi.com', name: 'Op', agent_role: 'owner', basis: 'Owns this agent' }),
    row({ user_id: 2, email: 'boss@dimagi.com', workspace_role: 'owner', agent_role: 'admin', basis: 'Owns the workspace' }),
    row({ user_id: 3, email: 'adm@dimagi.com', agent_role: 'admin', basis: 'Made admin by op@dimagi.com' }),
    row({ user_id: 4, email: 'ed@dimagi.com', full_rule: 'member@dimagi.com:verified' }),
    row({ user_id: 5, email: 'vw@partner.org', workspace_role: 'viewer', access: 'confined', capabilities: ['ask'] }),
    row({ user_id: 6, email: 'no@partner.org', workspace_role: 'viewer', access: 'none' }),
    row({ user_id: 7, email: 'ed2@dimagi.com', manual_only: true }),
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
    expect(await screen.findAllByTestId('agent-access-row')).toHaveLength(7)
    expect(within(rowFor('boss@dimagi.com')).getByText('Admin')).toBeTruthy()
    expect(within(rowFor('boss@dimagi.com')).getByText('Owns the workspace')).toBeTruthy()
    expect(within(rowFor('vw@partner.org')).getByText('Only: ask')).toBeTruthy()
    expect(within(rowFor('no@partner.org')).getByText('No access')).toBeTruthy()
    // the editor tier: the whole agent, but never in auto
    expect(within(rowFor('ed2@dimagi.com')).getByText('Whole agent, manual only')).toBeTruthy()
    expect(screen.getByText(/only ask/)).toBeTruthy()
    // the role is plain text, never a dropdown, for someone who may not manage admins
    expect(screen.queryAllByRole('combobox')).toHaveLength(0)
    expect(within(rowFor('ed@dimagi.com')).getByText('Member')).toBeTruthy()
  })

  it('a manager changes a role from the dropdown; the owner and workspace owners stay fixed', async () => {
    getAgentAccess.mockResolvedValue(ACCESS)
    grantAgentAdmin.mockResolvedValue([])
    revokeAgentAdmin.mockResolvedValue([])
    render(<AgentAccessRoster agentSlug="ace" canManage />)
    await screen.findAllByTestId('agent-access-row')

    // fixed rows: same column, plain text, with the reason under it
    expect(within(rowFor('op@dimagi.com')).queryByRole('combobox')).toBeNull()
    expect(within(rowFor('op@dimagi.com')).getByText('Owner')).toBeTruthy()
    expect(within(rowFor('op@dimagi.com')).getByText('Owns this agent')).toBeTruthy()
    expect(within(rowFor('boss@dimagi.com')).queryByRole('combobox')).toBeNull()
    // no link-buttons left
    expect(screen.queryByText('Make admin')).toBeNull()
    expect(screen.queryByText('Remove admin')).toBeNull()

    const ed = screen.getByLabelText('Change role for ed@dimagi.com') as HTMLSelectElement
    expect(ed.value).toBe('member')
    fireEvent.change(ed, { target: { value: 'admin' } })
    await waitFor(() => expect(grantAgentAdmin).toHaveBeenCalledWith('ace', 4))
    // the whole answer is re-read, since admin changes access as well as role
    await waitFor(() => expect(getAgentAccess).toHaveBeenCalledTimes(2))

    const adm = screen.getByLabelText('Change role for adm@dimagi.com') as HTMLSelectElement
    expect(adm.value).toBe('admin')
    fireEvent.change(adm, { target: { value: 'member' } })
    await waitFor(() => expect(revokeAgentAdmin).toHaveBeenCalledWith('ace', 3))
  })

  it('shows a failed change inline on the row', async () => {
    getAgentAccess.mockResolvedValue(ACCESS)
    grantAgentAdmin.mockRejectedValue(new Error('not allowed'))
    render(<AgentAccessRoster agentSlug="ace" canManage />)
    await screen.findAllByTestId('agent-access-row')
    fireEvent.change(screen.getByLabelText('Change role for ed@dimagi.com'), { target: { value: 'admin' } })
    expect(await within(rowFor('ed@dimagi.com')).findByText('not allowed')).toBeTruthy()
  })

  it('leaves outsiders to the Callers section when no caller rules are published', async () => {
    getAgentAccess.mockResolvedValue({ ...ACCESS, interface_published: false, outsiders: [] })
    render(<AgentAccessRoster agentSlug="ace" canManage={false} />)
    await screen.findAllByTestId('agent-access-row')
    expect(screen.queryByText(/Outside the workspace/)).toBeNull()
  })

  it('separates what was set on the agent from what the workspace role gives', async () => {
    getAgentAccess.mockResolvedValue(ACCESS)
    render(
      <MemoryRouter>
        <AgentAccessRoster agentSlug="ace" agentName="Ace" workspace="connect" canManage={false} />
      </MemoryRouter>,
    )
    await screen.findAllByTestId('agent-access-row')
    expect(screen.getByText('Set on Ace')).toBeTruthy()
    const inherited = screen.getByTestId('agent-access-inherited')
    expect(within(inherited).getByText(/From the workspace · 4 people/)).toBeTruthy()
    // grouped by what they reach, not one row each
    const groups = within(screen.getByTestId('agent-access-groups'))
    expect(groups.getAllByRole('listitem')).toHaveLength(4)
    expect(groups.getByText('Whole agent, manual only')).toBeTruthy()
    expect(groups.getByText('No access')).toBeTruthy()
    expect(within(inherited).getByRole('link', { name: /Manage workspace members/ }).getAttribute('href'))
      .toBe('/w/connect/settings/members')
  })
})
