// @vitest-environment jsdom
//
// A workspace vault's page (VaultPage.tsx): what it serves, who is in it and why, its key
// made in the browser, and — for an owner or admin — the controls. Values never appear.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const api = vi.hoisted(() => ({
  attachWorkspaceVault: vi.fn(), detachWorkspaceVault: vi.fn(), addWorkspaceVaultMember: vi.fn(),
  removeWorkspaceVaultMember: vi.fn(), deleteWorkspaceVaultSecret: vi.fn(), getWorkspaceVaultZk: vi.fn(),
}))
vi.mock('@/api/vault', () => api)
const ops = vi.hoisted(() => ({
  makeWorkspaceVaultKey: vi.fn(), grantWorkspaceVaultPending: vi.fn(), sealWorkspaceVaultValue: vi.fn(),
}))
vi.mock('@/vault/operations', () => ops)

const { VaultMembersTab, VaultOverviewTab, VaultSecretsTab } = await import('./VaultTabs')
const { vaultTabs } = await import('./VaultPage')

const SHARED = {
  vault_id: 'v1', workspace: 'dimagi', name: 'Shared', kv: 1, secrets: ['gog-oauth-client'],
  attachments: [{ id: 3, agent: null, workspace: 'dimagi' }], you: 'owner',
  holders: { has_key: true, people: ['jj@dimagi.com'], keepers: [] },
  members: [
    { user_id: 1, email: 'jj@dimagi.com', role: 'owner', sources: ['workspace_owners:dimagi'], has_vault_key: true },
    { user_id: 2, email: 'st@dimagi.com', role: 'member', sources: ['attached:ace'], has_vault_key: false },
  ],
}
const ME = { userId: '1' }

function ctx(over: Record<string, unknown> = {}) {
  return { vault: { ...SHARED, ...over } as never, onChange: vi.fn(), reload: vi.fn(), unlock: vi.fn().mockResolvedValue(ME) }
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('vault page', () => {
  it('marks Overview while it has no key', () => {
    expect(vaultTabs({ ...SHARED, kv: 0 } as never)[0].dot).toBe(true)
    expect(vaultTabs(SHARED as never)[0].dot).toBe(false)
  })

  it('says what it serves, a runner by a link to its page', () => {
    api.getWorkspaceVaultZk.mockResolvedValue({ pending: [] })
    render(<MemoryRouter><VaultOverviewTab {...ctx({ attachments: [
      { id: 3, agent: null, workspace: 'dimagi' }, { id: 4, agent: null, workspace: null, runner: 'r1', runner_name: 'cloud-ec2-2' },
    ] })} /></MemoryRouter>)
    const serves = screen.getByTestId('ws-vault-serves')
    expect(serves.textContent).toContain('every agent in dimagi and below')
    expect(screen.getByText('runner cloud-ec2-2').getAttribute('href')).toBe('/w/dimagi/runners/r1')
  })

  it('says per runner whether it can open the vault, and gives a copy to that runner only', async () => {
    const waiting = [
      { kind: 'device', target: 'k2', public_jwk: {}, runner: 'r2', runner_name: 'demo-a' },
      { kind: 'device', target: 'k3', public_jwk: {}, runner: 'r3', runner_name: 'demo-b' },
    ]
    api.getWorkspaceVaultZk.mockResolvedValue({ pending: waiting })
    ops.grantWorkspaceVaultPending.mockResolvedValue({})
    render(<MemoryRouter><VaultOverviewTab {...ctx({
      holders: { has_key: true, people: [], keepers: ['cloud-ec2-2'] },
      attachments: [
        { id: 4, runner: 'r1', runner_name: 'cloud-ec2-2' },
        { id: 5, runner: 'r2', runner_name: 'demo-a' },
        { id: 6, runner: 'r3', runner_name: 'demo-b' },
      ] })} /></MemoryRouter>)
    expect(screen.getByTestId('ws-vault-access-4').textContent).toBe('can open it')
    await waitFor(() => expect(screen.getByTestId('ws-vault-access-5').textContent).toContain('waiting for a copy'))
    // No all-at-once button for runners: each row grants its own.
    expect(screen.queryByTestId('vault-grant')).toBeNull()
    fireEvent.click(screen.getByTestId('ws-vault-grant-5'))
    await waitFor(() => expect(ops.grantWorkspaceVaultPending).toHaveBeenCalled())
    const only = ops.grantWorkspaceVaultPending.mock.calls[0][2]
    expect(waiting.filter(only).map((t) => t.runner)).toEqual(['r2'])
  })

  it('makes its key in the browser with one unlock', async () => {
    const c = ctx({ kv: 0 })
    render(<MemoryRouter><VaultOverviewTab {...c} /></MemoryRouter>)
    fireEvent.click(screen.getByTestId('vault-make-key'))
    await waitFor(() => expect(ops.makeWorkspaceVaultKey).toHaveBeenCalledWith('v1', ME))
    expect(c.reload).toHaveBeenCalled()
  })

  it('serves a whole workspace from "workspace:slug", an agent otherwise', async () => {
    api.getWorkspaceVaultZk.mockResolvedValue({ pending: [] })
    api.attachWorkspaceVault.mockResolvedValue(SHARED)
    render(<MemoryRouter><VaultOverviewTab {...ctx()} /></MemoryRouter>)
    const input = screen.getByLabelText('Serve an agent or workspace from Shared')
    fireEvent.change(input, { target: { value: 'workspace:connect' } })
    fireEvent.submit(screen.getByTestId('ws-vault-attach'))
    await waitFor(() => expect(api.attachWorkspaceVault).toHaveBeenCalledWith('v1', { workspace: 'connect' }))
    fireEvent.change(input, { target: { value: 'ace' } })
    fireEvent.submit(screen.getByTestId('ws-vault-attach'))
    await waitFor(() => expect(api.attachWorkspaceVault).toHaveBeenCalledWith('v1', { agent: 'ace' }))
  })

  it('says why each person is in it', () => {
    render(<VaultMembersTab {...ctx()} />)
    expect(screen.getByTestId('ws-vault-member-1').textContent).toContain('Owner of dimagi')
    expect(screen.getByTestId('ws-vault-member-2').textContent).toContain('Full access to ace')
    expect(screen.getByTestId('ws-vault-member-2').textContent).toContain('hasn’t set up their vault key')
  })

  it('seals a secret in the browser, by name only', async () => {
    const c = ctx()
    render(<VaultSecretsTab {...c} />)
    expect(screen.getByTestId('ws-vault-secrets').textContent).toContain('gog-oauth-client')
    fireEvent.change(screen.getByLabelText('Secret name'), { target: { value: 'OCS_TOKEN' } })
    fireEvent.change(screen.getByLabelText('Secret value'), { target: { value: 's3cret' } })
    fireEvent.submit(screen.getByTestId('ws-vault-seal'))
    await waitFor(() => expect(ops.sealWorkspaceVaultValue).toHaveBeenCalledWith('v1', 'OCS_TOKEN', 's3cret', ME))
    expect(c.reload).toHaveBeenCalled()
  })

  it('shows no controls to someone who only uses a vault', () => {
    api.getWorkspaceVaultZk.mockResolvedValue({ pending: [] })
    render(<MemoryRouter><VaultOverviewTab {...ctx({ you: 'member' })} /></MemoryRouter>)
    expect(screen.queryByTestId('ws-vault-attach')).toBeNull()
    expect(screen.queryByTestId('ws-vault-detach-3')).toBeNull()
    cleanup()
    render(<VaultMembersTab {...ctx({ you: 'member' })} />)
    expect(screen.queryByTestId('ws-vault-add-member')).toBeNull()
    cleanup()
    render(<VaultSecretsTab {...ctx({ you: 'member' })} />)
    expect(screen.queryByTestId('ws-vault-seal')).toBeNull()
  })
})
