// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AgentAccessOut } from '@/api/agents'

const grantAgentAdmin = vi.fn()
const revokeAgentAdmin = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  grantAgentAdmin: (...a: unknown[]) => grantAgentAdmin(...a),
  revokeAgentAdmin: (...a: unknown[]) => revokeAgentAdmin(...a),
}))

const { AgentAdmins } = await import('./AgentAdmins')

type Row = AgentAccessOut['members'][number]
const row = (o: Partial<Row>): Row => ({
  user_id: 1, email: 'x@dimagi.com', name: 'x@dimagi.com', workspace_role: 'editor', agent_role: 'member',
  basis: 'Workspace member', granted_at: null, access: 'full', capabilities: [], manual_only: false,
  may_request_auto: false, role: 'editor', source: 'workspace-editor', via: '', ...o,
})

const MEMBERS: Row[] = [
  row({ user_id: 1, email: 'op@dimagi.com', name: 'Op', agent_role: 'owner', basis: 'Owns this agent' }),
  row({ user_id: 2, email: 'boss@dimagi.com', workspace_role: 'owner', agent_role: 'admin', basis: 'Owns the workspace' }),
  row({ user_id: 3, email: 'adm@dimagi.com', name: 'Adm', agent_role: 'admin', basis: 'Made admin by op@dimagi.com' }),
  row({ user_id: 4, email: 'ed@dimagi.com' }),
  row({ user_id: 5, email: 'vw@partner.org', workspace_role: 'viewer', role: null }),
]

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function show(canManage: boolean, onChanged = vi.fn().mockResolvedValue(undefined)) {
  render(<AgentAdmins agentSlug="ace" agentName="Ace" members={MEMBERS} canManage={canManage} onChanged={onChanged} />)
  return onChanged
}

describe('AgentAdmins', () => {
  it('names the owner and the granted agent admins; a workspace owner is not a grant', () => {
    show(false)
    expect(screen.getAllByTestId('agent-admin-row').map((r) => r.textContent)).toEqual([
      'OwnerOp',
      'Agent adminAdm · Made admin by op@dimagi.com',
    ])
    // no controls for someone who may not manage agent admins
    expect(screen.queryByRole('button', { name: /Remove/ })).toBeNull()
    expect(screen.queryByRole('combobox')).toBeNull()
  })

  it('a manager revokes and makes agent admins, and the list is re-read after each', async () => {
    grantAgentAdmin.mockResolvedValue([])
    revokeAgentAdmin.mockResolvedValue([])
    const onChanged = show(true)
    fireEvent.click(screen.getByRole('button', { name: 'Remove adm@dimagi.com as agent admin' }))
    await waitFor(() => expect(revokeAgentAdmin).toHaveBeenCalledWith('ace', 3))
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1))

    const add = screen.getByRole('combobox', { name: 'Make an agent admin' }) as HTMLSelectElement
    // only plain members are offered: not the owner, nor anyone already an agent admin
    expect(Array.from(add.options).map((o) => o.value).filter(Boolean)).toEqual(['4', '5'])
    fireEvent.change(add, { target: { value: '4' } })
    fireEvent.click(screen.getByRole('button', { name: 'Make agent admin' }))
    await waitFor(() => expect(grantAgentAdmin).toHaveBeenCalledWith('ace', 4))
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(2))
  })

  it('shows a refusal inline', async () => {
    grantAgentAdmin.mockRejectedValue(new Error('not allowed'))
    show(true)
    fireEvent.change(screen.getByRole('combobox', { name: 'Make an agent admin' }), { target: { value: '4' } })
    fireEvent.click(screen.getByRole('button', { name: 'Make agent admin' }))
    expect((await screen.findByRole('alert')).textContent).toBe('not allowed')
  })
})
