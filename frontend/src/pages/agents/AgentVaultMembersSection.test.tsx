// @vitest-environment jsdom
//
// A vault's members, each with WHY they are one. Only an owner or admin sees the add
// form, the link toggle, and Remove — and Remove only on a direct membership,
// since a linked one goes with the person's access to the agent.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { getVaultMembers, addVaultMember, removeVaultMember, setVaultAgentLink } = vi.hoisted(() => ({
  getVaultMembers: vi.fn(),
  addVaultMember: vi.fn(),
  removeVaultMember: vi.fn(),
  setVaultAgentLink: vi.fn(),
}))
vi.mock('@/api/vault', () => ({ getVaultMembers, addVaultMember, removeVaultMember, setVaultAgentLink }))

const { AgentVaultMembersSection } = await import('./AgentVaultMembersSection')

const STATE = {
  vault_id: 'v1', kind: 'agent', name: 'ace', agent_link: { linked: true, role: 'member' }, you: 'owner',
  members: [
    { user_id: 1, email: 'jj@dimagi.com', role: 'owner', sources: ['agent:ace'], has_vault_key: true },
    { user_id: 2, email: 'tm@dimagi.com', role: 'member', sources: ['direct'], has_vault_key: false },
  ],
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentVaultMembersSection', () => {
  it('says why each person is a member', async () => {
    getVaultMembers.mockResolvedValue(STATE)
    render(<AgentVaultMembersSection slug="ace" />)
    expect(await screen.findByText('jj@dimagi.com')).toBeTruthy()
    expect(screen.getByTestId('vault-member-1').textContent).toContain('Full access to this agent')
    expect(screen.getByTestId('vault-member-2').textContent).toContain('Added by name')
    expect(screen.getByTestId('vault-member-2').textContent).toContain('hasn’t set up their vault key')
  })

  it('lets a manager remove only a direct membership', async () => {
    getVaultMembers.mockResolvedValue(STATE)
    removeVaultMember.mockResolvedValue({ ...STATE, members: [STATE.members[0]] })
    render(<AgentVaultMembersSection slug="ace" />)
    await screen.findByText('jj@dimagi.com')
    expect(screen.queryByTestId('vault-remove-1')).toBeNull()
    fireEvent.click(screen.getByTestId('vault-remove-2'))
    await waitFor(() => expect(removeVaultMember).toHaveBeenCalledWith('ace', 2))
    await waitFor(() => expect(screen.queryByText('tm@dimagi.com')).toBeNull())
  })

  it('shows no controls to someone who does not manage the vault', async () => {
    getVaultMembers.mockResolvedValue({ ...STATE, you: 'member' })
    render(<AgentVaultMembersSection slug="ace" />)
    await screen.findByText('jj@dimagi.com')
    expect(screen.queryByTestId('vault-add-form')).toBeNull()
    expect(screen.queryByTestId('vault-toggle-link')).toBeNull()
    expect(screen.queryByTestId('vault-remove-2')).toBeNull()
  })
})
