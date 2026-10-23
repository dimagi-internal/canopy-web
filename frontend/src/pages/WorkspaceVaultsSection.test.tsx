// @vitest-environment jsdom
//
// A workspace's vaults: what each serves, who is in it and why, and — for an owner or admin —
// the controls. Values never appear: only their names, and the CLI line that sets one.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const api = vi.hoisted(() => ({
  listWorkspaceVaults: vi.fn(), createWorkspaceVault: vi.fn(), attachWorkspaceVault: vi.fn(),
  detachWorkspaceVault: vi.fn(), addWorkspaceVaultMember: vi.fn(), removeWorkspaceVaultMember: vi.fn(),
  getWorkspaceVault: vi.fn(),
}))
vi.mock('@/api/vault', () => api)
const ops = vi.hoisted(() => ({ grantPendingWorkspace: vi.fn(async () => ({})), unlockVaultKey: vi.fn() }))
vi.mock('@/vault/operations', () => ops)
vi.mock('@/auth/AuthProvider', () => ({ useAuth: () => ({ status: 'authenticated', user: { email: 'jj@dimagi.com' } }) }))

const { WorkspaceVaultsSection } = await import('./WorkspaceVaultsSection')

const SHARED = {
  vault_id: 'v1', workspace: 'dimagi', name: 'Shared', kv: 1, secrets: ['gog-oauth-client'],
  attachments: [{ id: 3, agent: null, workspace: 'dimagi' }], you: 'owner',
  members: [
    { user_id: 1, email: 'jj@dimagi.com', role: 'owner', sources: ['workspace_owners:dimagi'], has_vault_key: true },
    { user_id: 2, email: 'st@dimagi.com', role: 'member', sources: ['attached:ace'], has_vault_key: false },
  ],
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('WorkspaceVaultsSection', () => {
  it('says what a vault serves and why each person is in it', async () => {
    api.listWorkspaceVaults.mockResolvedValue([SHARED])
    render(<WorkspaceVaultsSection slug="dimagi" />)
    expect(await screen.findByText('Shared')).toBeTruthy()
    expect(screen.getByTestId('ws-vault-serves').textContent).toContain('every agent in dimagi and below')
    expect(screen.getByTestId('ws-vault-member-1').textContent).toContain('Owner of dimagi')
    expect(screen.getByTestId('ws-vault-member-2').textContent).toContain('Full access to ace')
    expect(screen.getByTestId('ws-vault-member-2').textContent).toContain('hasn’t set up their vault key')
    expect(screen.getByTestId('ws-vault-cli').textContent).toContain('canopy vault zk set --vault v1')
  })

  it('serves a whole workspace from "workspace:slug", an agent otherwise', async () => {
    api.listWorkspaceVaults.mockResolvedValue([SHARED])
    api.attachWorkspaceVault.mockResolvedValue(SHARED)
    render(<WorkspaceVaultsSection slug="dimagi" />)
    await screen.findByText('Shared')
    const input = screen.getByLabelText('Serve an agent or workspace from Shared')
    fireEvent.change(input, { target: { value: 'workspace:connect' } })
    fireEvent.submit(screen.getByTestId('ws-vault-attach'))
    await waitFor(() => expect(api.attachWorkspaceVault).toHaveBeenCalledWith('v1', { workspace: 'connect' }))
    fireEvent.change(input, { target: { value: 'ace' } })
    fireEvent.submit(screen.getByTestId('ws-vault-attach'))
    await waitFor(() => expect(api.attachWorkspaceVault).toHaveBeenCalledWith('v1', { agent: 'ace' }))
  })

  it('shows no controls to someone who only uses a vault', async () => {
    api.listWorkspaceVaults.mockResolvedValue([{ ...SHARED, you: 'member' }])
    render(<WorkspaceVaultsSection slug="dimagi" />)
    await screen.findByText('Shared')
    expect(screen.queryByTestId('ws-vault-attach')).toBeNull()
    expect(screen.queryByTestId('ws-vault-add-member')).toBeNull()
    expect(screen.queryByTestId('ws-vault-detach-3')).toBeNull()
  })

  it('lets an owner who holds the key give it to whoever is waiting', async () => {
    const waiting = {
      ...SHARED,
      holders: { has_key: true, people: ['jj@dimagi.com'], keepers: [] },
      members: [
        { ...SHARED.members[0], has_copy: true },
        { ...SHARED.members[1], has_vault_key: true, has_copy: false },
      ],
    }
    api.listWorkspaceVaults.mockResolvedValue([waiting])
    api.getWorkspaceVault.mockResolvedValue({ ...waiting, members: waiting.members.map((m) => ({ ...m, has_copy: true })) })
    render(<WorkspaceVaultsSection slug="dimagi" />)
    fireEvent.click(await screen.findByTestId('ws-vault-give-access-v1'))
    await waitFor(() => expect(ops.grantPendingWorkspace).toHaveBeenCalledWith('v1', expect.any(Function)))
    await waitFor(() => expect(screen.queryByTestId('ws-vault-give-access-v1')).toBeNull())
  })
})
