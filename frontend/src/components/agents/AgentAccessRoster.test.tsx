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
    row({ user_id: 3, email: 'adm@dimagi.com', name: 'Adm', agent_role: 'admin', basis: 'Made admin by op@dimagi.com' }),
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

function show(canManage: boolean) {
  return render(
    <MemoryRouter>
      <AgentAccessRoster agentSlug="ace" agentName="Ace" workspace="connect" canManage={canManage} />
    </MemoryRouter>,
  )
}

async function expandAll() {
  fireEvent.click(await screen.findByTestId('agent-access-show-all'))
  return screen.findAllByTestId('agent-access-row')
}

describe('AgentAccessRoster', () => {
  it('leads with the rule inherited from the workspace, not a row per person', async () => {
    getAgentAccess.mockResolvedValue(ACCESS)
    show(false)
    const inherited = await screen.findByTestId('agent-access-inherited')
    expect(inherited.textContent).toContain('Inherited from the connect workspace (7 people)')
    expect(within(inherited).getByRole('link', { name: /Manage in workspace/ }).getAttribute('href'))
      .toBe('/w/connect/settings/members')
    const legend = screen.getAllByTestId('agent-access-legend').map((r) => r.textContent)
    expect(legend).toEqual([
      'Workspace owners→ Admin of Ace',
      'Editors & admins→ Can use Ace; turns need approval',
      'Viewers→ What the caller rules allow',
    ])
    // the full roster waits behind the expander
    expect(screen.queryAllByTestId('agent-access-row')).toHaveLength(0)
    expect(screen.getByTestId('agent-access-show-all').textContent).toContain('Show all 7 people')
    // the outsiders box and the Slack line live under "Who can reach it" now
    expect(screen.queryByText(/Outside the workspace/)).toBeNull()
    expect(screen.queryByText(/Slack:/)).toBeNull()
  })

  it('viewers get no access when no caller rules are published', async () => {
    getAgentAccess.mockResolvedValue({ ...ACCESS, interface_published: false, outsiders: [] })
    show(false)
    const legend = await screen.findAllByTestId('agent-access-legend')
    expect(legend[2].textContent).toBe('Viewers→ No access')
  })

  it('lists only what is set on the agent: its owner and granted admins', async () => {
    getAgentAccess.mockResolvedValue(ACCESS)
    show(false)
    const rows = (await screen.findAllByTestId('agent-access-exception')).map((r) => r.textContent)
    // the workspace owner (boss) is an admin by inheritance, so it is not an exception
    expect(rows).toEqual(['OwnerOp', 'AdminAdm · Made admin by op@dimagi.com'])
    // no controls for someone who may not manage admins
    expect(screen.queryByRole('button', { name: /Remove/ })).toBeNull()
    expect(screen.queryByRole('combobox', { name: 'Add admin' })).toBeNull()
  })

  it('a manager removes and adds admins from the exception list', async () => {
    getAgentAccess.mockResolvedValue(ACCESS)
    grantAgentAdmin.mockResolvedValue([])
    revokeAgentAdmin.mockResolvedValue([])
    show(true)
    fireEvent.click(await screen.findByRole('button', { name: 'Remove adm@dimagi.com as admin' }))
    await waitFor(() => expect(revokeAgentAdmin).toHaveBeenCalledWith('ace', 3))
    await waitFor(() => expect(getAgentAccess).toHaveBeenCalledTimes(2))

    const add = screen.getByRole('combobox', { name: 'Add admin' }) as HTMLSelectElement
    // only members are offered: not the owner, nor anyone already an admin
    const offered = Array.from(add.options).map((o) => o.value).filter(Boolean)
    expect(offered).toEqual(['4', '5', '6', '7'])
    fireEvent.change(add, { target: { value: '4' } })
    fireEvent.click(screen.getByRole('button', { name: 'Make admin' }))
    await waitFor(() => expect(grantAgentAdmin).toHaveBeenCalledWith('ace', 4))
  })

  it('the expander shows every person with role, reason and what they reach', async () => {
    getAgentAccess.mockResolvedValue(ACCESS)
    show(false)
    expect(await expandAll()).toHaveLength(7)
    expect(within(rowFor('boss@dimagi.com')).getByText('Admin')).toBeTruthy()
    expect(within(rowFor('boss@dimagi.com')).getByText('Owns the workspace')).toBeTruthy()
    expect(within(rowFor('vw@partner.org')).getByText('Only: ask')).toBeTruthy()
    expect(within(rowFor('no@partner.org')).getByText('No access')).toBeTruthy()
    // the editor tier: the whole agent, but never in auto
    expect(within(rowFor('ed2@dimagi.com')).getByText('Whole agent, manual only')).toBeTruthy()
    // the role is plain text, never a dropdown, for someone who may not manage admins
    expect(screen.queryAllByRole('combobox')).toHaveLength(0)
    expect(within(rowFor('ed@dimagi.com')).getByText('Member')).toBeTruthy()
  })

  it('in the full table a manager changes a role; the owner and workspace owners stay fixed', async () => {
    getAgentAccess.mockResolvedValue(ACCESS)
    grantAgentAdmin.mockResolvedValue([])
    revokeAgentAdmin.mockResolvedValue([])
    show(true)
    await expandAll()

    expect(within(rowFor('op@dimagi.com')).queryByRole('combobox')).toBeNull()
    expect(within(rowFor('op@dimagi.com')).getByText('Owner')).toBeTruthy()
    expect(within(rowFor('op@dimagi.com')).getByText('Owns this agent')).toBeTruthy()
    expect(within(rowFor('boss@dimagi.com')).queryByRole('combobox')).toBeNull()

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
    show(true)
    await expandAll()
    fireEvent.change(screen.getByLabelText('Change role for ed@dimagi.com'), { target: { value: 'admin' } })
    expect(await within(rowFor('ed@dimagi.com')).findByText('not allowed')).toBeTruthy()
  })
})
