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
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const { getAgentCredentialStatus, getAgentVault, setAgentVault, setAgentCredentials,
        deleteAgentCredential, startGoogleMint, revealAgentCredential, AgentApiError } = vi.hoisted(() => {
  class AgentApiError extends Error {
    status: number
    detail: string
    constructor(message: string, status: number, detail: string) {
      super(message)
      this.status = status
      this.detail = detail
    }
  }
  return {
    getAgentCredentialStatus: vi.fn(),
    getAgentVault: vi.fn(),
    setAgentVault: vi.fn(),
    setAgentCredentials: vi.fn(),
    deleteAgentCredential: vi.fn(),
    startGoogleMint: vi.fn(),
    revealAgentCredential: vi.fn(),
    AgentApiError,
  }
})

vi.mock('@/api/agents', () => ({
  getAgentCredentialStatus, getAgentVault, setAgentVault, setAgentCredentials,
  deleteAgentCredential, startGoogleMint, revealAgentCredential, AgentApiError,
  // The GitHub and Salesforce sections have their own tests; here they only have to not throw.
  getAgentGitHub: () => new Promise(() => {}),
  getAgentSalesforce: () => new Promise(() => {}),
}))

// Device keys + the read log have their own tests (DeviceKeysTable.test.tsx).
const { getCredentialAlerts, getPatRotation, setPatRotation, rotatePatNow } = vi.hoisted(() => ({
  getCredentialAlerts: vi.fn(),
  getPatRotation: vi.fn(),
  setPatRotation: vi.fn(),
  rotatePatNow: vi.fn(),
}))
vi.mock('@/api/deviceKeys', () => ({
  getAgentDeviceKeys: () => Promise.resolve([]),
  setDeviceBinding: vi.fn(),
  getCredentialReads: () => Promise.resolve([]),
  approveRunnerDeviceKey: vi.fn(),
  revokeRunnerDeviceKey: vi.fn(),
  getCredentialAlerts, getPatRotation, setPatRotation, rotatePatNow,
}))

const ROTATION = {
  enabled: false, period_days: 30, last_rotated_at: null, next_due_at: null,
  enabled_by_email: '', history: [],
}

const { AgentCredentialsPanel } = await import('./AgentCredentialsPanel')

const AGENT = { slug: 'echo', workspace: 'connect' }

function show() {
  return render(
    <MemoryRouter>
      <AgentCredentialsPanel agent={AGENT} />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  getCredentialAlerts.mockResolvedValue([])
  getPatRotation.mockResolvedValue(ROTATION)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentCredentialsPanel — Vault Phase 1b (canopy-web#1549)', () => {
  it('turns canopy-pat rotation on with the chosen period', async () => {
    getAgentCredentialStatus.mockResolvedValue([])
    getAgentVault.mockResolvedValue({ vault: '', key_set: false, declared: 0, locatable: 0 })
    setPatRotation.mockResolvedValue({ ...ROTATION, enabled: true, period_days: 14,
                                       next_due_at: '2026-11-01T00:00:00Z', enabled_by_email: 'jj@dimagi.com' })
    show()
    const days = (await screen.findByTestId('pat-rotation-days')) as HTMLInputElement
    fireEvent.change(days, { target: { value: '14' } })
    fireEvent.click(screen.getByTestId('pat-rotation-enabled'))
    await waitFor(() => expect(setPatRotation).toHaveBeenCalledWith('echo', true, 14))
    await waitFor(() =>
      expect(screen.getByTestId('pat-rotation-summary').textContent).toContain('on by jj@dimagi.com'))
  })

  it('lists read alerts, and shows those naming a ref in its read history', async () => {
    getAgentCredentialStatus.mockResolvedValue([
      { name: 'canopy-pat', declared: true, set: true, source: 'canopy-web', updated_at: null,
        last_read_at: '2026-10-16T00:00:00Z', read_count: 3 },
    ])
    getAgentVault.mockResolvedValue({ vault: '', key_set: false, declared: 1, locatable: 0 })
    getCredentialAlerts.mockResolvedValue([
      { id: 1, kind: 'no-turn', detail: 'a box read outside a turn', refs: ['canopy-pat'], user_email: '',
        runner: '', runner_name: '', turn_id: '', occurrences: 4, notified: true,
        created_at: '2026-10-16T00:00:00Z', last_seen_at: '2026-10-16T00:10:00Z' },
    ])
    show()
    const section = await screen.findByTestId('agent-credential-alerts')
    expect(within(section).getByText('Read outside a turn')).toBeTruthy()
    expect(within(section).getByText('4×')).toBeTruthy()
    fireEvent.click(screen.getByTestId('cred-reads-canopy-pat'))
    expect(await screen.findByTestId('cred-alerts-canopy-pat')).toBeTruthy()
  })

  it('shows neither to someone who cannot edit', async () => {
    getAgentCredentialStatus.mockResolvedValue([])
    getAgentVault.mockResolvedValue({ vault: '', key_set: false, declared: 0, locatable: 0 })
    render(
      <MemoryRouter>
        <AgentCredentialsPanel agent={AGENT} canEdit={false} />
      </MemoryRouter>,
    )
    await screen.findByTestId('agent-credentials')
    expect(screen.queryByTestId('agent-pat-rotation')).toBeNull()
    expect(getCredentialAlerts).not.toHaveBeenCalled()
  })
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
    // Declaring nothing is not narrated any more (#1314): that lives in docs.
    expect(screen.queryByTestId('agent-credentials-undeclared')).toBeNull()
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

  it('offers "Connect Google mailbox" to an agent with a mailbox that declares nothing', async () => {
    // ada/echo/eva/hal on 2026-10-04: declared 0, mailbox set — and no button,
    // so re-minting their mailbox needed a terminal and a 1Password write.
    getAgentCredentialStatus.mockResolvedValue([])
    getAgentVault.mockResolvedValue({ vault: 'Agent-Ada', key_set: true, declared: 0, locatable: 0 })
    render(
      <MemoryRouter>
        <AgentCredentialsPanel agent={{ ...AGENT, email: 'echo@dimagi-ai.com' }} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByText('Connect Google mailbox')).toBeTruthy())
  })

  it('offers no mailbox button to an agent with neither a mailbox nor a gog-token', async () => {
    getAgentCredentialStatus.mockResolvedValue([])
    getAgentVault.mockResolvedValue({ vault: '', key_set: false, declared: 0, locatable: 0 })
    show()
    await waitFor(() => expect(screen.getByTestId('agent-vault')).toBeTruthy())
    expect(screen.queryByText('Connect Google mailbox')).toBeNull()
    expect(screen.queryByTestId('credential-status-mailbox')).toBeNull()
  })

  it('points at the workspace shared vault, which is the other level', async () => {
    getAgentCredentialStatus.mockResolvedValue([])
    getAgentVault.mockResolvedValue({ vault: '', key_set: false, declared: 0, locatable: 0 })
    show()
    await waitFor(() => expect(screen.getByTestId('shared-vault-link')).toBeTruthy())
    expect(screen.getByTestId('shared-vault-link').getAttribute('href'))
      .toBe('/w/connect/settings/secrets')
  })

  it('leads with a status list, the unconnected mailbox and its action on one line', async () => {
    getAgentCredentialStatus.mockResolvedValue([])
    getAgentVault.mockResolvedValue({ vault: 'Agent-Echo', key_set: true, declared: 0, locatable: 0 })
    render(
      <MemoryRouter>
        <AgentCredentialsPanel agent={{ ...AGENT, email: 'echo@dimagi-ai.com' }} />
      </MemoryRouter>,
    )
    const vault = await screen.findByTestId('credential-status-vault')
    await waitFor(() => expect(vault.textContent).toContain('Set'))
    const mailbox = screen.getByTestId('credential-status-mailbox')
    expect(mailbox.textContent).toContain('Not connected')
    expect(within(mailbox).getByRole('button', { name: 'Connect Google mailbox' })).toBeTruthy()
    // the status list comes before every detail section
    const list = screen.getByTestId('credential-status')
    expect(list.compareDocumentPosition(screen.getByTestId('agent-vault')) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy()
    // "Needs a person" is gone: the status line carries it
    expect(screen.queryByTestId('needs-you')).toBeNull()
  })

  it('reads a stored gog-token as a connected mailbox', async () => {
    getAgentCredentialStatus.mockResolvedValue([
      { name: 'gog-token', declared: true, set: true, source: 'canopy', updated_at: null },
    ])
    getAgentVault.mockResolvedValue({ vault: '', key_set: false, declared: 1, locatable: 0 })
    show()
    expect((await screen.findByTestId('credential-status-mailbox')).textContent).toContain('Connected')
    await waitFor(() => expect(screen.getByTestId('credential-status-vault').textContent).toContain('Not set'))
  })

  it('says how values are held once, and who may change them only to someone who cannot', async () => {
    getAgentCredentialStatus.mockResolvedValue([])
    getAgentVault.mockResolvedValue({ vault: '', key_set: false, declared: 0, locatable: 0 })
    render(
      <MemoryRouter>
        <AgentCredentialsPanel agent={{ ...AGENT, name: 'Echo' }} canEdit={false} />
      </MemoryRouter>,
    )
    const note = await screen.findByTestId('credentials-note')
    expect(note.textContent).toContain('asks for your passkey')
    expect(note.textContent).toContain('Only Echo’s admins can change or reveal them.')
    expect(screen.getAllByText(/encrypted at rest/i)).toHaveLength(1)
  })

  it('reveals one stored value on demand, and only to someone who may edit', async () => {
    getAgentCredentialStatus.mockResolvedValue([
      { name: 'nova-api-key', declared: true, set: true, source: 'canopy', updated_at: null },
    ])
    getAgentVault.mockResolvedValue({ vault: '', key_set: false, declared: 1, locatable: 0 })
    revealAgentCredential.mockResolvedValue('the-plaintext')
    show()
    const btn = await screen.findByTestId('reveal-nova-api-key')
    expect(screen.queryByTestId('revealed-nova-api-key')).toBeNull()
    fireEvent.click(btn)
    await waitFor(() => expect(screen.getByTestId('revealed-nova-api-key').textContent).toContain('the-plaintext'))
    expect(revealAgentCredential).toHaveBeenCalledWith('echo', 'nova-api-key')
    // Hide drops it from the page.
    fireEvent.click(screen.getByTestId('reveal-nova-api-key'))
    expect(screen.queryByTestId('revealed-nova-api-key')).toBeNull()
  })

  it('shows the server’s reason when a reveal is refused', async () => {
    getAgentCredentialStatus.mockResolvedValue([
      { name: 'nova-api-key', declared: true, set: true, source: 'canopy', updated_at: null },
    ])
    getAgentVault.mockResolvedValue({ vault: '', key_set: false, declared: 1, locatable: 0 })
    revealAgentCredential.mockRejectedValue(
      new AgentApiError('revealAgentCredential failed: {...}', 403, 'Confirm it’s you with your passkey, then try again.'),
    )
    show()
    fireEvent.click(await screen.findByTestId('reveal-nova-api-key'))
    expect((await screen.findByTestId('agent-credentials-error')).textContent).toBe(
      'Confirm it’s you with your passkey, then try again.',
    )
  })

  it('offers no Reveal to a viewer who cannot edit', async () => {
    getAgentCredentialStatus.mockResolvedValue([
      { name: 'nova-api-key', declared: true, set: true, source: 'canopy', updated_at: null },
    ])
    getAgentVault.mockResolvedValue({ vault: '', key_set: false, declared: 1, locatable: 0 })
    render(
      <MemoryRouter>
        <AgentCredentialsPanel agent={AGENT} canEdit={false} />
      </MemoryRouter>,
    )
    await screen.findByTestId('cred-nova-api-key')
    expect(screen.queryByTestId('reveal-nova-api-key')).toBeNull()
  })

  it('stores a file from disk as a value, without a trailing newline', async () => {
    getAgentCredentialStatus.mockResolvedValue([
      { name: 'GOOGLE_PROVISIONER_KEY', declared: true, set: false, source: 'unset', updated_at: null },
    ])
    getAgentVault.mockResolvedValue({ vault: '', key_set: false, declared: 1, locatable: 0 })
    setAgentCredentials.mockResolvedValue([])
    render(
      <MemoryRouter>
        <AgentCredentialsPanel agent={{ ...AGENT, credential_source: 'canopy-web' }} />
      </MemoryRouter>,
    )
    const input = await screen.findByTestId('upload-GOOGLE_PROVISIONER_KEY')
    const key = '{\n  "type": "service_account"\n}\n'
    fireEvent.change(input, { target: { files: [new File([key], 'key.json')] } })
    await waitFor(() => expect(setAgentCredentials).toHaveBeenCalledWith(
      'echo', { GOOGLE_PROVISIONER_KEY: '{\n  "type": "service_account"\n}' }))
  })

  it('leads with an unset ref on canopy vault instead of filing it under 1Password', async () => {
    getAgentCredentialStatus.mockResolvedValue([
      { name: 'GOOGLE_PROVISIONER_KEY', declared: true, set: false, source: 'unset', updated_at: null },
    ])
    getAgentVault.mockResolvedValue({ vault: '', key_set: false, declared: 1, locatable: 0 })
    render(
      <MemoryRouter>
        <AgentCredentialsPanel agent={{ ...AGENT, credential_source: 'canopy-web' }} />
      </MemoryRouter>,
    )
    const section = await screen.findByTestId('not-set')
    expect(within(section).getByTestId('cred-GOOGLE_PROVISIONER_KEY')).toBeTruthy()
    expect(screen.queryByTestId('toggle-vault-refs')).toBeNull()
    expect(screen.getByTestId('agent-credentials-summary').textContent).toMatch(/not set yet/)
  })
})
