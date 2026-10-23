// @vitest-environment jsdom
//
// A workspace's vaults, one row each, each linking to its own page (VaultTabs.test.tsx
// covers what is on it). Values never appear: only names and counts.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const api = vi.hoisted(() => ({ listWorkspaceVaults: vi.fn(), createWorkspaceVault: vi.fn() }))
vi.mock('@/api/vault', () => api)

const { WorkspaceVaultsSection } = await import('./WorkspaceVaultsSection')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('WorkspaceVaultsSection', () => {
  it('lists each vault, what it serves, and links to its page', async () => {
    api.listWorkspaceVaults.mockResolvedValue([
      { vault_id: 'v1', workspace: 'dimagi', name: 'Shared', kv: 1, secrets: ['gog-oauth-client'],
        attachments: [{ id: 3, agent: null, workspace: 'dimagi' }], you: 'owner', members: [] },
      { vault_id: 'v2', workspace: 'dimagi', name: 'cloud-ec2-2 Claude credentials', kv: 0, secrets: [],
        attachments: [{ id: 4, agent: null, workspace: null, runner: 'r1', runner_name: 'cloud-ec2-2' }], you: 'owner', members: [] },
    ])
    render(<MemoryRouter><WorkspaceVaultsSection slug="dimagi" /></MemoryRouter>)
    const shared = await screen.findByText('Shared')
    expect(shared.getAttribute('href')).toBe('/w/dimagi/vaults/v1')
    expect(screen.getByTestId('ws-vault-v1').textContent).toContain('every agent in dimagi and below')
    expect(screen.getByTestId('ws-vault-v2').textContent).toContain('runner cloud-ec2-2')
    expect(screen.getByTestId('ws-vault-v2').textContent).toContain('none yet')
  })
})
