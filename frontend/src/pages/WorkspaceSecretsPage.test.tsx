// @vitest-environment jsdom
//
// The tenant half of the credential model. It had a backend, generated types and
// no UI at all — so "where does this secret live" was answerable for an agent
// and unanswerable for a workspace, and four agents in `connect` ran for weeks
// with nothing registered because nothing on any screen said they should be.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import { WorkspaceApiError, type SharedVaultOut } from '@/api/workspaces'

// vi.hoisted: the mock factories below are hoisted above these declarations.
const { getSharedVault, setSharedVault, listAgents } = vi.hoisted(() => ({
  getSharedVault: vi.fn<(slug: string) => Promise<SharedVaultOut>>(),
  setSharedVault: vi.fn<(slug: string, body: unknown) => Promise<SharedVaultOut>>(),
  listAgents: vi.fn(),
}))

vi.mock('@/api/workspaces', async (orig) => ({
  ...(await orig<typeof import('@/api/workspaces')>()),
  getSharedVault,
  setSharedVault,
}))
vi.mock('@/api/agents', () => ({ listAgents }))

const { WorkspaceSecretsPage } = await import('./WorkspaceSecretsPage')

function show() {
  return render(
    <MemoryRouter initialEntries={['/w/connect/settings/secrets']}>
      <Routes>
        <Route path="/w/:workspace/settings/secrets" element={<WorkspaceSecretsPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('WorkspaceSecretsPage', () => {
  it('says a vault with no service account cannot be read', async () => {
    getSharedVault.mockResolvedValue({ vault: 'Canopy-Shared', key_set: false })
    listAgents.mockResolvedValue({ items: [], total: 0, offset: 0, limit: 0 })
    show()
    await waitFor(() =>
      expect(screen.getByTestId('shared-vault-state').textContent).toContain('No service account'))
    // The consequence, not just the state — this is the failure it prevents.
    expect(screen.getByTestId('shared-vault-state').textContent).toContain('will not authenticate')
  })

  it('shows a stored service account as set, and never shows a value', async () => {
    getSharedVault.mockResolvedValue({ vault: 'Canopy-Shared', key_set: true })
    listAgents.mockResolvedValue({ items: [], total: 0, offset: 0, limit: 0 })
    show()
    await waitFor(() =>
      expect(screen.getByTestId('shared-vault-state').textContent).toContain('is stored'))
    const key = screen.getByTestId('shared-vault-key') as HTMLInputElement
    expect(key.type).toBe('password')
    expect(key.value).toBe('')
    expect(key.placeholder).toContain('rotate')
  })

  it('omits a blank key so renaming the vault cannot wipe it', async () => {
    getSharedVault.mockResolvedValue({ vault: 'Old-Name', key_set: true })
    setSharedVault.mockResolvedValue({ vault: 'New-Name', key_set: true })
    listAgents.mockResolvedValue({ items: [], total: 0, offset: 0, limit: 0 })
    show()
    await waitFor(() => expect(screen.getByTestId('shared-vault-save')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Vault'), { target: { value: 'New-Name' } })
    fireEvent.click(screen.getByTestId('shared-vault-save'))
    await waitFor(() => expect(setSharedVault).toHaveBeenCalled())
    expect(setSharedVault).toHaveBeenCalledWith('connect', { vault: 'New-Name' })
  })

  it('sends a pasted key and clears it from the form', async () => {
    getSharedVault.mockResolvedValue({ vault: 'Canopy-Shared', key_set: false })
    setSharedVault.mockResolvedValue({ vault: 'Canopy-Shared', key_set: true })
    listAgents.mockResolvedValue({ items: [], total: 0, offset: 0, limit: 0 })
    show()
    await waitFor(() => expect(screen.getByTestId('shared-vault-save')).toBeTruthy())
    fireEvent.change(screen.getByTestId('shared-vault-key'), { target: { value: ' ops_secret ' } })
    fireEvent.click(screen.getByTestId('shared-vault-save'))
    await waitFor(() => expect(screen.getByTestId('shared-vault-saved')).toBeTruthy())
    expect(setSharedVault).toHaveBeenCalledWith('connect',
      { vault: 'Canopy-Shared', service_key: 'ops_secret' })
    expect((screen.getByTestId('shared-vault-key') as HTMLInputElement).value).toBe('')
    expect(screen.getByTestId('shared-vault-state').textContent).toContain('is stored')
  })

  it('states the model: 1Password holds secrets, canopy-web holds the service account', async () => {
    getSharedVault.mockResolvedValue({ vault: '', key_set: false })
    listAgents.mockResolvedValue({ items: [], total: 0, offset: 0, limit: 0 })
    show()
    await waitFor(() => expect(screen.getByTestId('workspace-secrets')).toBeTruthy())
    const text = screen.getByTestId('workspace-secrets').textContent ?? ''
    expect(text).toContain('1Password')
    expect(text).toContain('service account')
    expect(text).toContain('holds the keys, not the passwords')
    // And names the other two levels, which is the confusion this page fixes.
    expect(text).toContain('Each agent’s own vault')
    expect(text).toContain('What a runner itself holds')
  })

  it('links to each agent in this workspace for its own vault', async () => {
    getSharedVault.mockResolvedValue({ vault: 'Canopy-Shared', key_set: true })
    listAgents.mockResolvedValue({
      items: [
        { slug: 'hal', name: 'Hal', workspace: 'connect' },
        { slug: 'eva', name: 'Eva', workspace: 'dimagi' },   // another tenant
      ],
      total: 2, offset: 0, limit: 20,
    })
    show()
    await waitFor(() => expect(screen.getByTestId('agent-vault-link-hal')).toBeTruthy())
    expect(screen.getByTestId('agent-vault-link-hal').getAttribute('href'))
      .toBe('/w/connect/agents/hal/overview#credentials')
    expect(screen.queryByTestId('agent-vault-link-eva')).toBeNull()
  })

  it('tells a non-owner it is owner-only rather than showing a form that 403s', async () => {
    getSharedVault.mockRejectedValue(new WorkspaceApiError(403, 'forbidden'))
    listAgents.mockResolvedValue({ items: [], total: 0, offset: 0, limit: 0 })
    show()
    await waitFor(() => expect(screen.getByTestId('secrets-forbidden')).toBeTruthy())
    expect(screen.queryByTestId('shared-vault-save')).toBeNull()
  })
})
