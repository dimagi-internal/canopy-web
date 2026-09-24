// @vitest-environment jsdom
//
// The vault form has to be reachable for an agent that has nothing yet — which
// is the only kind of agent anyone needs it for.
//
// It was rendered inside the "this agent declares secrets" branch, so on
// ada/echo/eva/hal (all `declared: 0`) the panel showed one sentence about
// runtime.yaml and no form at all. ace, which declares 45 refs, showed it —
// so the screen looked right to whoever built it and was empty for every agent
// waiting to be registered. Reported 2026-09-23: "I don't even see where the
// 1pass service account goes".
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const { getAgentCredentialStatus, getAgentVault, setAgentVault, setAgentCredentials,
        deleteAgentCredential, startGoogleMint } = vi.hoisted(() => ({
  getAgentCredentialStatus: vi.fn(),
  getAgentVault: vi.fn(),
  setAgentVault: vi.fn(),
  setAgentCredentials: vi.fn(),
  deleteAgentCredential: vi.fn(),
  startGoogleMint: vi.fn(),
}))

vi.mock('@/api/agents', () => ({
  getAgentCredentialStatus, getAgentVault, setAgentVault, setAgentCredentials,
  deleteAgentCredential, startGoogleMint,
  // The GitHub section has its own test; here it only has to not throw.
  getAgentGitHub: () => new Promise(() => {}),
}))

const { AgentCredentialsPanel } = await import('./AgentCredentialsPanel')

const AGENT = { slug: 'echo', workspace: 'connect' }

function show() {
  return render(
    <MemoryRouter>
      <AgentCredentialsPanel agent={AGENT} />
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentCredentialsPanel', () => {
  it('offers the vault + service account for an agent that declares nothing', async () => {
    getAgentCredentialStatus.mockResolvedValue([])          // echo, today
    getAgentVault.mockResolvedValue({ vault: '', key_set: false, declared: 0, locatable: 0 })
    show()
    await waitFor(() => expect(screen.getByTestId('agent-vault')).toBeTruthy())
    expect(screen.getByLabelText('Vault')).toBeTruthy()
    const key = screen.getByLabelText('Service key') as HTMLInputElement
    expect(key.type).toBe('password')
    // And it still says the declaration is separate, rather than implying the
    // vault is pointless.
    expect(screen.getByTestId('agent-credentials-undeclared').textContent)
      .toContain('Setting the vault above is still worth doing')
  })

  it('still offers it for an agent that declares refs', async () => {
    getAgentCredentialStatus.mockResolvedValue([
      { name: 'gog-token', declared: true, set: false, source: '1password', updated_at: null },
    ])
    getAgentVault.mockResolvedValue({ vault: 'Agent-Ace', key_set: true, declared: 45, locatable: 44 })
    show()
    await waitFor(() => expect(screen.getByTestId('agent-vault')).toBeTruthy())
    expect(screen.getByTestId('agent-credentials-summary')).toBeTruthy()
  })

  it('points at the workspace shared vault, which is the other level', async () => {
    getAgentCredentialStatus.mockResolvedValue([])
    getAgentVault.mockResolvedValue({ vault: '', key_set: false, declared: 0, locatable: 0 })
    show()
    await waitFor(() => expect(screen.getByTestId('shared-vault-link')).toBeTruthy())
    expect(screen.getByTestId('shared-vault-link').getAttribute('href'))
      .toBe('/w/connect/settings/secrets')
  })
})
