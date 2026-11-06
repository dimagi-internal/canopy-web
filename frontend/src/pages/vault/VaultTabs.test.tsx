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
const dir = vi.hoisted(() => ({
  agents: [
    { slug: 'ace', name: 'Ace', workspace: 'dimagi' },
    { slug: 'otto', name: 'otto', workspace: 'connect' },
    { slug: 'stray', name: 'Stray', workspace: 'elsewhere' },
  ],
  projects: [{ id: 1, workspace: 'dimagi', handle: 'canopy-support', address: 'dimagi/canopy-support', name: 'Canopy support' }],
  people: [], labels: [],
}))
vi.mock('@/components/work/useDirectory', () => ({ useDirectory: () => dir }))
vi.mock('@/api/workspaces', () => ({
  listWorkspaces: vi.fn().mockResolvedValue([
    { slug: 'dimagi', display_name: 'Dimagi', parent: null },
    { slug: 'connect', display_name: 'Connect', parent: 'dimagi' },
    { slug: 'elsewhere', display_name: 'Elsewhere', parent: null },
  ]),
}))

const { VaultMachinesTab, VaultMakeKey, VaultPeopleTab, VaultSecretsTab, VaultServedTab } = await import('./VaultTabs')

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

const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('vault page', () => {
  it('lists each kind on its own tab; its runners are machines, not on any of them', async () => {
    const c = ctx({ attachments: [
      { id: 3, agent: null, workspace: 'dimagi' }, { id: 4, agent: null, workspace: null, runner: 'r1', runner_name: 'cloud-ec2-2' },
    ] })
    render(<MemoryRouter>
      <VaultServedTab {...c} kind="workspaces" /><VaultServedTab {...c} kind="agents" /><VaultServedTab {...c} kind="projects" />
    </MemoryRouter>)
    await waitFor(() => expect(screen.getByTestId('ws-vault-serves-workspaces').textContent).toContain('Dimagi and below'))
    expect(screen.getByTestId('ws-vault-serves-agents').textContent).toContain('No agent gets this vault on its own.')
    for (const k of ['workspaces', 'agents', 'projects']) {
      expect(screen.getByTestId(`ws-vault-serves-${k}`).textContent).not.toContain('cloud-ec2-2')
    }
  })

  it('says which of a project\u2019s member agents it serves and which it cannot, and attaches "project:ws/handle"', async () => {
    api.getWorkspaceVaultZk.mockResolvedValue({ pending: [] })
    api.attachWorkspaceVault.mockResolvedValue(SHARED)
    render(<MemoryRouter><VaultServedTab kind="projects" {...ctx({ attachments: [
      { id: 5, agent: null, workspace: null, project: 'dimagi/canopy-support', served: ['ada', 'hal'], not_served: ['lab'],
        project_outside_tree: false },
    ] })} /></MemoryRouter>)
    const row = screen.getByTestId('ws-vault-project-5').textContent
    expect(screen.getByTestId('ws-vault-serves-projects').textContent).toContain('Canopy support')
    expect(row).toContain('serves ada, hal')
    expect(row).toContain("not served (outside the vault's tree): lab")
  })

  it('lists its runners under Machines: where each key lives, its access, and Give access for that runner only', async () => {
    const waiting = [
      { kind: 'device', target: 'k2', public_jwk: {}, runner: 'r2', runner_name: 'demo-a' },
      { kind: 'device', target: 'k3', public_jwk: {}, runner: 'r3', runner_name: 'demo-b' },
    ]
    ops.grantWorkspaceVaultPending.mockResolvedValue({})
    const c = ctx({
      attachments: [
        { id: 3, agent: null, workspace: 'dimagi' },
        { id: 4, runner: 'r1', runner_name: 'cloud-ec2-2', access: 'has', key_place: 'aws-kms' },
        { id: 5, runner: 'r2', runner_name: 'demo-a', access: 'waiting', key_place: 'kms-broker' },
        { id: 6, runner: 'r3', runner_name: 'demo-b', access: 'none', key_place: '' },
      ] })
    render(<MemoryRouter><VaultMachinesTab {...c} /></MemoryRouter>)
    expect(screen.getByTestId('ws-vault-runner-4').textContent).toContain('key held by AWS')
    expect(screen.getByTestId('ws-vault-access-4').textContent).toBe('has access')
    expect(screen.getByTestId('ws-vault-runner-5').textContent).toContain('key held by AWS')
    expect(screen.getByTestId('ws-vault-access-6').textContent).toContain('no access yet')
    fireEvent.click(screen.getByTestId('ws-vault-grant-5'))
    await waitFor(() => expect(ops.grantWorkspaceVaultPending).toHaveBeenCalled())
    const only = ops.grantWorkspaceVaultPending.mock.calls[0][2]
    expect(waiting.filter(only).map((t) => t.runner)).toEqual(['r2'])
    expect(c.reload).toHaveBeenCalled()
  })

  it('makes its key in the browser with one unlock', async () => {
    const c = ctx({ kv: 0 })
    render(<MemoryRouter><VaultMakeKey {...c} /></MemoryRouter>)
    fireEvent.click(screen.getByTestId('vault-make-key'))
    await waitFor(() => expect(ops.makeWorkspaceVaultKey).toHaveBeenCalledWith('v1', ME))
    expect(c.reload).toHaveBeenCalled()
  })

  it('names who gets a copy only once they set up a vault key', () => {
    render(<MemoryRouter><VaultMakeKey {...ctx({ kv: 0, members: [
      { user_id: 1, email: 'lead@dimagi.com', role: 'owner', sources: [], has_vault_key: true, has_copy: false },
      { user_id: 12, email: 'amie@dimagi.com', role: 'admin', sources: [], has_vault_key: false, has_copy: false },
    ] })} /></MemoryRouter>)
    expect(screen.getByTestId('vault-no-key-later').textContent).toBe(
      'amie@dimagi.com has not set up a vault key yet, so gets a copy once they do.')
  })

  it('gives an agent, project or workspace picked from real lists in the vault\u2019s tree, saying first what it gives', async () => {
    api.attachWorkspaceVault.mockResolvedValue(SHARED)
    render(<MemoryRouter><VaultServedTab kind="agents" {...ctx({ attachments: [] })} /></MemoryRouter>)
    expect(screen.queryByRole('textbox')).toBeNull()
    fireEvent.click(screen.getByLabelText('Give an agent Shared'))
    expect(await screen.findByText('Ace')).toBeTruthy()
    expect(screen.queryByText('Stray')).toBeNull()
    fireEvent.click(await screen.findByText('otto'))
    await waitFor(() => expect(api.attachWorkspaceVault).toHaveBeenCalledWith('v1', { agent: 'otto' }))
    expect(confirm.mock.calls[0][0]).toContain('Give otto Shared?')
    expect(confirm.mock.calls[0][0]).toContain('It gets: gog-oauth-client.')
    cleanup()
    render(<MemoryRouter><VaultServedTab kind="projects" {...ctx({ attachments: [] })} /></MemoryRouter>)
    fireEvent.click(screen.getByLabelText('Give a project Shared'))
    fireEvent.click(await screen.findByText('Canopy support'))
    await waitFor(() => expect(api.attachWorkspaceVault).toHaveBeenCalledWith('v1', { project: 'dimagi/canopy-support' }))
    cleanup()
    render(<MemoryRouter><VaultServedTab kind="workspaces" {...ctx({ attachments: [] })} /></MemoryRouter>)
    fireEvent.click(await screen.findByLabelText('Give a whole workspace Shared'))
    fireEvent.click(await screen.findByText('Connect'))
    await waitFor(() => expect(api.attachWorkspaceVault).toHaveBeenCalledWith('v1', { workspace: 'connect' }))
  })

  it('gives nothing when the reader backs out of the confirm', async () => {
    confirm.mockReturnValueOnce(false)
    render(<MemoryRouter><VaultServedTab kind="agents" {...ctx({ attachments: [] })} /></MemoryRouter>)
    fireEvent.click(screen.getByLabelText('Give an agent Shared'))
    fireEvent.click(await screen.findByText('otto'))
    await waitFor(() => expect(confirm).toHaveBeenCalled())
    expect(api.attachWorkspaceVault).not.toHaveBeenCalled()
  })

  it('does not offer what it already serves', async () => {
    render(<MemoryRouter><VaultServedTab kind="agents" {...ctx({ attachments: [{ id: 7, agent: 'ace', workspace: null }] })} /></MemoryRouter>)
    expect(screen.getByTestId('ws-vault-serves-agents').textContent).toContain('Ace')
    fireEvent.click(screen.getByLabelText('Give an agent Shared'))
    expect(await screen.findByText('otto')).toBeTruthy()
    expect(screen.getAllByText('Ace')).toHaveLength(1)
  })

  it('names the machines holding a copy of the key', () => {
    render(<MemoryRouter><VaultMachinesTab {...ctx({ holders: { has_key: true, people: [], keepers: ['cloud-ec2-live'] } })} /></MemoryRouter>)
    expect(screen.getByTestId('ws-vault-keepers').textContent).toContain('cloud-ec2-live')
  })

  it('says why each person is in it', () => {
    render(<VaultPeopleTab {...ctx()} />)
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
    render(<MemoryRouter><VaultServedTab kind="workspaces" {...ctx({ you: 'member' })} /></MemoryRouter>)
    expect(screen.queryByTestId('ws-vault-attach-workspaces')).toBeNull()
    expect(screen.queryByTestId('ws-vault-detach-3')).toBeNull()
    cleanup()
    render(<VaultPeopleTab {...ctx({ you: 'member' })} />)
    expect(screen.queryByTestId('ws-vault-add-member')).toBeNull()
    cleanup()
    render(<VaultSecretsTab {...ctx({ you: 'member' })} />)
    expect(screen.queryByTestId('ws-vault-seal')).toBeNull()
  })
})
