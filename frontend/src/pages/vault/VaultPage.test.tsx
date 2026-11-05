// @vitest-environment jsdom
//
// A workspace vault's page (VaultPage.tsx) on the shared VaultView: the same summary line and
// sections an agent's vault has, and the old tab addresses open the section they became.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const api = vi.hoisted(() => ({ getWorkspaceVault: vi.fn(), getWorkspaceVaultZk: vi.fn() }))
vi.mock('@/api/vault', () => api)
vi.mock('@/vault/operations', () => ({}))
vi.mock('@/vault/useVaultUnlock', () => ({ useVaultUnlock: () => ({ unlock: vi.fn(), element: null }) }))
vi.mock('@/vault/VaultWaiting', () => ({ memberWaiting: () => null, VaultWaitingNote: () => null }))

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
  it('opens on a summary of what is in the vault and who can open it', async () => {
    show()
    expect((await screen.findByTestId('vault-summary')).textContent)
      .toBe('Canopy vault · canopy-web can’t read it · 2 secrets · 2 people · 1 machine')
    expect(screen.getByTestId('vault-section-secrets-summary').textContent).toBe('2 stored')
    expect(screen.getByTestId('vault-section-access-to-secrets-summary').textContent).toBe('2 people · 1 machine')
  })

  it('starts on its secrets, with the rest closed', async () => {
    show()
    await screen.findByTestId('vault-view')
    expect([open('secrets'), open('access-to-secrets'), open('vault-storage')]).toEqual([true, false, false])
  })

  it('opens the section an old tab address meant', async () => {
    show('/w/dimagi/vaults/v1/members')
    await screen.findByTestId('vault-view')
    expect([open('secrets'), open('access-to-secrets'), open('vault-storage')]).toEqual([false, true, false])
    cleanup()
    show('/w/dimagi/vaults/v1/overview')
    await screen.findByTestId('vault-view')
    expect([open('secrets'), open('access-to-secrets'), open('vault-storage')]).toEqual([false, false, true])
  })

  it('puts making its key first, above the sections, when it has none', async () => {
    show('/w/dimagi/vaults/v1', { ...VAULT, kv: 0 })
    const step = await screen.findByTestId('vault-first-step')
    expect(step.textContent).toContain('First, make this vault’s key')
    expect(step.compareDocumentPosition(screen.getByTestId('vault-section-secrets-summary'))
      & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.queryByTestId('vault-attention')).toBeNull()
    expect(screen.getByTestId('vault-section-vault-storage-summary').textContent).toBe('no key yet')
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
    expect(open('access-to-secrets')).toBe(true)
  })
})
