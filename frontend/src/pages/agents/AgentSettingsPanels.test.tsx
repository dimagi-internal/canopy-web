// @vitest-environment jsdom
//
// The credential panels of an agent's Settings: its secrets and who can open
// them (the Security tab), and its External accounts.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const { getAgentCredentialStatus, setAgentCredentials,
        deleteAgentCredential, startGoogleMint, revealZkValue, AgentApiError } = vi.hoisted(() => {
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
    setAgentCredentials: vi.fn(),
    deleteAgentCredential: vi.fn(),
    startGoogleMint: vi.fn(),
    revealZkValue: vi.fn(),
    AgentApiError,
  }
})

vi.mock('@/api/agents', () => ({
  getAgentCredentialStatus, setAgentCredentials,
  deleteAgentCredential, startGoogleMint, AgentApiError, getGoogleMint: vi.fn(),
  // The GitHub section has its own tests; here it only has to not throw.
  getAgentGitHub: () => new Promise(() => {}),
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
  giveCanopyLogin: vi.fn(),
  getCredentialReads: () => Promise.resolve([]),
  approveRunnerDeviceKey: vi.fn(),
  revokeRunnerDeviceKey: vi.fn(),
  getCredentialAlerts, getPatRotation, setPatRotation, rotatePatNow,
}))
// Reveal opens the value in the browser with the person's vault key (`revealZkValue`,
// tested in src/vault); here it only has to be called.
vi.mock('@/vault/operations', async (orig) => ({
  ...(await orig<typeof import('@/vault/operations')>()),
  revealZkValue,
}))
// No vault key on the agent yet: Save goes through the passkey-gated route.
vi.mock('@/api/vault', async (orig) => ({
  ...(await orig<typeof import('@/api/vault')>()),
  getZkState: () => Promise.reject(new Error('off')),
  getKeyset: () => Promise.resolve({ exists: true }),
}))
vi.mock('@/api/mail', () => ({ getAgentMailbox: () => Promise.reject(new Error('none')) }))

const ROTATION = {
  enabled: false, period_days: 30, last_rotated_at: null, next_due_at: null,
  enabled_by_email: '', history: [],
}

const { AgentSecretsPanel } = await import('./AgentSecretsPanel')
const { AgentAccountsPanel } = await import('./AgentAccountsPanel')
const { AgentSecurityPanel } = await import('./AgentSecurityPanel')

const AGENT = { slug: 'echo', workspace: 'connect' }

const inRouter = (ui: React.ReactNode) => render(<MemoryRouter>{ui}</MemoryRouter>)

beforeEach(() => {
  getCredentialAlerts.mockResolvedValue([])
  getPatRotation.mockResolvedValue(ROTATION)
  getAgentCredentialStatus.mockResolvedValue([])
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('Security — its secrets', () => {
  it('changes a key in its row, through the passkey-gated route', async () => {
    getAgentCredentialStatus.mockResolvedValue([
      { name: 'NOVA_API_KEY', declared: true, set: true, source: 'canopy-web', updated_at: null },
    ])
    setAgentCredentials.mockResolvedValue([
      { name: 'NOVA_API_KEY', declared: true, set: true, source: 'canopy-web', updated_at: '2026-10-22T00:00:00Z' },
    ])
    inRouter(<AgentSecretsPanel agent={AGENT} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Change NOVA_API_KEY' }))
    fireEvent.change(screen.getByLabelText('New value for NOVA_API_KEY'), { target: { value: 'new-key' } })
    fireEvent.click(screen.getByTestId('vault-save-NOVA_API_KEY'))
    await waitFor(() => expect(setAgentCredentials).toHaveBeenCalledWith('echo', { NOVA_API_KEY: 'new-key' }))
  })

  it('stores a file from disk as a value on Save, without a trailing newline', async () => {
    getAgentCredentialStatus.mockResolvedValue([
      { name: 'GOOGLE_PROVISIONER_KEY', declared: true, set: false, source: 'unset', updated_at: null },
    ])
    setAgentCredentials.mockResolvedValue([])
    inRouter(<AgentSecretsPanel agent={AGENT} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Set GOOGLE_PROVISIONER_KEY' }))
    const key = '{\n  "type": "service_account"\n}\n'
    fireEvent.change(screen.getByTestId('upload-GOOGLE_PROVISIONER_KEY'), { target: { files: [new File([key], 'key.json')] } })
    await waitFor(() => expect((screen.getByLabelText('New value for GOOGLE_PROVISIONER_KEY') as HTMLTextAreaElement).value)
      .toBe('{\n  "type": "service_account"\n}'))
    fireEvent.click(screen.getByTestId('vault-save-GOOGLE_PROVISIONER_KEY'))
    await waitFor(() => expect(setAgentCredentials).toHaveBeenCalledWith(
      'echo', { GOOGLE_PROVISIONER_KEY: '{\n  "type": "service_account"\n}' }))
  })

  it('says what needs you: a declared key with no value, a stored one nothing declares', async () => {
    getAgentCredentialStatus.mockResolvedValue([
      { name: 'NOVA_API_KEY', declared: true, set: false, source: '', updated_at: null },
      { name: 'OLD_TOKEN', declared: false, set: true, source: 'canopy-web', updated_at: null },
      { name: 'OCS_API_TOKEN', declared: true, set: true, source: 'canopy-web', updated_at: null },
    ])
    inRouter(<AgentSecretsPanel agent={AGENT} />)
    const summary = await screen.findByTestId('agent-credentials-summary')
    expect(summary.textContent).toContain('1 stored secret(s) nothing declares')
    expect(summary.textContent).toContain('1 declared secret(s) have no value')
    expect(within(screen.getByTestId('vault-row-NOVA_API_KEY')).getByText('no value')).toBeTruthy()
    expect(within(screen.getByTestId('vault-row-OLD_TOKEN')).getByText('not declared')).toBeTruthy()
  })

  it('reveals one stored value on demand, and hides it again', async () => {
    getAgentCredentialStatus.mockResolvedValue([
      { name: 'nova-api-key', declared: true, set: true, source: 'canopy', updated_at: null },
    ])
    revealZkValue.mockResolvedValue('the-plaintext')
    inRouter(<AgentSecretsPanel agent={AGENT} />)
    fireEvent.click(await screen.findByTestId('reveal-nova-api-key'))
    await waitFor(() => expect(screen.getByTestId('revealed-nova-api-key').textContent).toContain('the-plaintext'))
    expect(revealZkValue).toHaveBeenCalledWith('echo', 'nova-api-key', expect.any(Function))
    fireEvent.click(screen.getByTestId('reveal-nova-api-key'))
    expect(screen.queryByTestId('revealed-nova-api-key')).toBeNull()
  })

  it('shows the server’s reason when a reveal is refused', async () => {
    getAgentCredentialStatus.mockResolvedValue([
      { name: 'nova-api-key', declared: true, set: true, source: 'canopy', updated_at: null },
    ])
    revealZkValue.mockRejectedValue(
      new AgentApiError('getZkState failed: {...}', 403, 'Confirm it’s you with your passkey, then try again.'),
    )
    inRouter(<AgentSecretsPanel agent={AGENT} />)
    fireEvent.click(await screen.findByTestId('reveal-nova-api-key'))
    expect((await screen.findByTestId('vault-error')).textContent).toBe(
      'Confirm it’s you with your passkey, then try again.',
    )
  })

  it('offers a viewer who cannot edit no Reveal, and says who can', async () => {
    getAgentCredentialStatus.mockResolvedValue([
      { name: 'nova-api-key', declared: true, set: true, source: 'canopy', updated_at: null },
    ])
    inRouter(<AgentSecretsPanel agent={{ ...AGENT, name: 'Echo' }} canEdit={false} />)
    await screen.findByTestId('vault-row-nova-api-key')
    expect(screen.queryByTestId('reveal-nova-api-key')).toBeNull()
    const note = screen.getByTestId('credentials-note')
    expect(note.textContent).toContain('asks for your passkey')
    expect(note.textContent).toContain('Only Echo’s admins can change or reveal them.')
    expect(getCredentialAlerts).not.toHaveBeenCalled()
  })

  it('shows read alerts naming a key in its read history', async () => {
    getAgentCredentialStatus.mockResolvedValue([
      { name: 'canopy-pat', declared: true, set: true, source: 'canopy-web', updated_at: null,
        last_read_at: '2026-10-16T00:00:00Z', read_count: 3 },
    ])
    getCredentialAlerts.mockResolvedValue([
      { id: 1, kind: 'no-turn', detail: 'a box read outside a turn', refs: ['canopy-pat'], user_email: '',
        runner: '', runner_name: '', turn_id: '', occurrences: 4, notified: true,
        created_at: '2026-10-16T00:00:00Z', last_seen_at: '2026-10-16T00:10:00Z' },
    ])
    inRouter(<AgentSecretsPanel agent={AGENT} />)
    await waitFor(() => expect(screen.getByTestId('cred-reads-canopy-pat').textContent).toContain('1 alert'))
    fireEvent.click(screen.getByTestId('cred-reads-canopy-pat'))
    expect(await screen.findByTestId('cred-alerts-canopy-pat')).toBeTruthy()
  })
})

describe('External accounts', () => {
  it('leads with a status list, the unconnected mailbox and its action on one line', async () => {
    // ada/echo/eva/hal on 2026-10-04: declared 0, mailbox set — and no button,
    // so re-minting their mailbox needed a terminal and a hand-written secret.
    getAgentCredentialStatus.mockResolvedValue([])
    inRouter(<AgentAccountsPanel agent={{ ...AGENT, email: 'echo@dimagi-ai.com' }} />)
    const mailbox = await screen.findByTestId('credential-status-mailbox')
    expect(mailbox.textContent).toContain('Not connected')
    expect(within(mailbox).getByRole('button', { name: 'Connect Google mailbox' })).toBeTruthy()
    // The list is the first thing in the tab (the outcome banner aside).
    expect(screen.getByTestId('agent-accounts').firstElementChild).toBe(screen.getByTestId('credential-status'))
  })

  it('offers no mailbox button to an agent with neither a mailbox nor a gog-token', async () => {
    getAgentCredentialStatus.mockResolvedValue([])
    inRouter(<AgentAccountsPanel agent={AGENT} />)
    await screen.findByTestId('credential-status-github')
    expect(screen.queryByText('Connect Google mailbox')).toBeNull()
    expect(screen.queryByTestId('credential-status-mailbox')).toBeNull()
  })

  it('reads a stored gog-token as a connected mailbox', async () => {
    getAgentCredentialStatus.mockResolvedValue([
      { name: 'gog-token', declared: true, set: true, source: 'canopy', updated_at: null },
    ])
    inRouter(<AgentAccountsPanel agent={AGENT} />)
    expect((await screen.findByTestId('credential-status-mailbox')).textContent).toContain('Connected')
  })
})

describe('Security — about the agent', () => {
  it('turns canopy-login renewal on with the chosen period', async () => {
    setPatRotation.mockResolvedValue({ ...ROTATION, enabled: true, period_days: 14,
                                       next_due_at: '2026-11-01T00:00:00Z', enabled_by_email: 'jj@dimagi.com' })
    inRouter(<AgentSecurityPanel agent={AGENT} canEdit />)
    const days = (await screen.findByTestId('pat-rotation-days')) as HTMLInputElement
    fireEvent.change(days, { target: { value: '14' } })
    fireEvent.click(screen.getByTestId('pat-rotation-enabled'))
    await waitFor(() => expect(setPatRotation).toHaveBeenCalledWith('echo', true, 14))
    await waitFor(() =>
      expect(screen.getByTestId('pat-rotation-summary').textContent).toContain('on by jj@dimagi.com'))
  })

  it('lists read alerts', async () => {
    getCredentialAlerts.mockResolvedValue([
      { id: 1, kind: 'no-turn', detail: 'a box read outside a turn', refs: ['canopy-pat'], user_email: '',
        runner: '', runner_name: '', turn_id: '', occurrences: 4, notified: true,
        created_at: '2026-10-16T00:00:00Z', last_seen_at: '2026-10-16T00:10:00Z' },
    ])
    getAgentCredentialStatus.mockResolvedValue([])
    inRouter(<AgentSecretsPanel agent={AGENT} />)
    const section = await screen.findByTestId('agent-credential-alerts')
    expect(within(section).getByText('Read outside a turn')).toBeTruthy()
    expect(within(section).getByText('4×')).toBeTruthy()
  })

  it('shows neither to someone who cannot edit', async () => {
    inRouter(<AgentSecurityPanel agent={AGENT} canEdit={false} />)
    await screen.findByTestId('agent-security')
    expect(screen.queryByTestId('agent-pat-rotation')).toBeNull()
    expect(getCredentialAlerts).not.toHaveBeenCalled()
  })
})
