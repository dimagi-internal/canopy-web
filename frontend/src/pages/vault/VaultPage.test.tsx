// @vitest-environment jsdom
//
// A workspace vault's page (VaultPage.tsx) on the shared VaultView: the same summary line and
// sections an agent's vault has, and the old tab addresses open the section they became.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const api = vi.hoisted(() => ({ getWorkspaceVault: vi.fn(), getWorkspaceVaultZk: vi.fn(), getWorkspaceVaultLog: vi.fn() }))
vi.mock('@/api/vault', () => api)
vi.mock('@/vault/operations', () => ({}))
vi.mock('@/vault/useVaultUnlock', () => ({ useVaultUnlock: () => ({ unlock: vi.fn(), element: null }) }))
vi.mock('@/vault/VaultWaiting', () => ({ memberWaiting: () => null, VaultWaitingNote: () => null }))
vi.mock('@/components/work/useDirectory', () => ({ useDirectory: () => ({ agents: [], projects: [], people: [], labels: [] }) }))
vi.mock('@/api/workspaces', () => ({ listWorkspaces: vi.fn().mockResolvedValue([]) }))

const { VaultPage } = await import('./VaultPage')

const VAULT = {
  vault_id: 'v1', workspace: 'dimagi', name: 'Shared', kv: 1, secrets: ['gog-oauth-client', 'nova-key'],
  attachments: [{ id: 4, agent: null, workspace: null, runner: 'r1', runner_name: 'cloud-ec2-2', access: 'has' }],
  you: 'owner', holders: { has_key: true, people: ['jj@dimagi.com'], keepers: [] },
  members: [
    { user_id: 1, email: 'jj@dimagi.com', role: 'owner', sources: ['workspace_owners:dimagi'], has_vault_key: true, has_copy: true },
    { user_id: 2, email: 'st@dimagi.com', role: 'member', sources: ['direct'], has_vault_key: true, has_copy: true },
  ],
}

function show(at = '/w/dimagi/vaults/v1', vault: object = VAULT) {
  api.getWorkspaceVault.mockResolvedValue(vault)
  api.getWorkspaceVaultZk.mockResolvedValue({ pending: [] })
  api.getWorkspaceVaultLog.mockResolvedValue([
    { at: '2026-11-06T15:00:00Z', kind: 'change', who: 'jj@dimagi.com', what: 'Attached otto', where: 'browser', turn_id: '' },
    { at: '2026-11-06T14:00:00Z', kind: 'read', who: 'eva', what: 'read salesforce', where: 'cloud-ec2-live', turn_id: 'abcdef123' },
  ])
  return render(
    <MemoryRouter initialEntries={[at]}>
      <Routes>
        <Route path="/w/:workspace/vaults/:vaultId" element={<VaultPage />} />
        <Route path="/w/:workspace/vaults/:vaultId/:tab" element={<VaultPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

const open = (id: string) => screen.getByTestId(`vault-section-${id}`).getAttribute('aria-expanded') === 'true'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('workspace vault page', () => {
  it('shows its managers a Log of changes and reads, and nobody else', async () => {
    show('/w/dimagi/vaults/v1/log')
    const rows = await screen.findAllByTestId('vault-log-row')
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining('jj@dimagi.comAttached ottobrowser'),
      expect.stringContaining('evaread salesforcecloud-ec2-live · turn abcdef12'),
    ])
    expect(open('vault-log')).toBe(true)
    fireEvent.click(screen.getByTestId('vault-log-only-read'))
    expect(screen.getAllByTestId('vault-log-row')).toHaveLength(1)
    cleanup()
    show('/w/dimagi/vaults/v1', { ...VAULT, you: 'member' })
    await screen.findByTestId('vault-summary')
    expect(screen.queryByTestId('vault-section-vault-log')).toBeNull()
    expect(api.getWorkspaceVaultLog).toHaveBeenCalledTimes(1)
  })

  it('has Secrets, Access (a tab per kind), Encryption key — the same sections an agent\u2019s vault has', async () => {
    show()
    await screen.findByTestId('vault-summary')
    expect(screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent?.split(/(?=[a-z0-9]+ ·|gog)/)[0]))
      .toEqual(expect.arrayContaining([expect.stringContaining('Secrets'), expect.stringContaining('Access'),
        expect.stringContaining('Encryption key')]))
    expect(screen.getAllByRole('tab', { hidden: true }).map((t) => t.textContent))
      .toEqual(['People2', 'Agents0', 'Projects0', 'Workspaces0', 'Machines1'])
    expect(screen.getByTestId('vault-key-explainer').textContent).toContain('locked with one key')
  })

  it('opens on a summary of what is in the vault and who can open it', async () => {
    show()
    expect((await screen.findByTestId('vault-summary')).textContent)
      .toBe('Canopy vault · canopy-web can’t read it · 2 secrets · 2 people · 1 machine')
    expect(screen.getByTestId('vault-section-secrets-summary').textContent).toBe('gog-oauth-client, nova-key')
    expect(screen.getByTestId('vault-section-access-summary').textContent).toBe('2 people · 1 machine')
  })

  it('starts on its secrets, with the rest closed', async () => {
    show()
    await screen.findByTestId('vault-view')
    expect([open('secrets'), open('access'), open('encryption-key')]).toEqual([true, false, false])
  })

  it('opens the section an old tab address meant', async () => {
    show('/w/dimagi/vaults/v1/members')
    await screen.findByTestId('vault-view')
    expect([open('secrets'), open('access'), open('encryption-key')]).toEqual([false, true, false])
    cleanup()
    show('/w/dimagi/vaults/v1/overview')
    await screen.findByTestId('vault-view')
    expect([open('secrets'), open('access'), open('encryption-key')]).toEqual([false, false, true])
  })

  it('puts making its key first, above the sections, when it has none', async () => {
    show('/w/dimagi/vaults/v1', { ...VAULT, kv: 0 })
    const step = await screen.findByTestId('vault-first-step')
    expect(step.textContent).toContain('First, make this vault’s key')
    expect(step.compareDocumentPosition(screen.getByTestId('vault-section-secrets-summary'))
      & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByTestId('vault-attention')).toBeNull()
    expect(screen.getByTestId('vault-section-encryption-key-summary').textContent).toBe('no key yet')
  })

  it('shows no first step once it has a key', async () => {
    show('/w/dimagi/vaults/v1')
    await screen.findByTestId('vault-view')
    expect(screen.queryByTestId('vault-first-step')).toBeNull()
  })

  it('says who is waiting for the key', async () => {
    const waiting = { ...VAULT, members: [{ ...VAULT.members[0] }, { ...VAULT.members[1], has_copy: false }] }
    show('/w/dimagi/vaults/v1', waiting)
    expect((await screen.findByTestId('vault-attention')).textContent).toContain('1 person is waiting for the key')
    expect(open('access')).toBe(true)
  })
})
