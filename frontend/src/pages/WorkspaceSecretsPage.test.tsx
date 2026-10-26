// @vitest-environment jsdom
//
// The tenant half of the credential model: where every secret an agent uses lives.
// All of it is canopy vaults now — a workspace vault for what agents share, each
// agent's own vault, a runner's own login — and no 1Password key anywhere.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const { listAgents } = vi.hoisted(() => ({ listAgents: vi.fn() }))

vi.mock('@/api/agents', () => ({ listAgents }))
vi.mock('./WorkspaceVaultsSection', () => ({ WorkspaceVaultsSection: () => <div data-testid="workspace-vaults" /> }))

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
  it('states the model: canopy vaults at every level, and no 1Password key', async () => {
    listAgents.mockResolvedValue({ items: [], total: 0, offset: 0, limit: 0 })
    show()
    await waitFor(() => expect(screen.getByTestId('workspace-secrets')).toBeTruthy())
    const text = screen.getByTestId('workspace-secrets').textContent ?? ''
    expect(text).toContain('canopy vault')
    expect(text).not.toContain('1Password')
    expect(screen.getByTestId('workspace-vaults')).toBeTruthy()
    expect(text).toContain('Each agent’s own vault')
    expect(text).toContain('What a runner itself holds')
  })

  it('links to each agent in this workspace for its own vault', async () => {
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
      .toBe('/w/connect/agents/hal/settings/security')
    expect(screen.queryByTestId('agent-vault-link-eva')).toBeNull()
  })
})
