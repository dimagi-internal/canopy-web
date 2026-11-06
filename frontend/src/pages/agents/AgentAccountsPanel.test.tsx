// @vitest-environment jsdom
//
// The External accounts tab is ONE list: a row per account, every row alike
// (service name | status and one short detail | one action).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AuthContext } from '@/auth/AuthProvider'

const m = vi.hoisted(() => ({
  getAgentSharedVaults: vi.fn(async () => []),
  getAgentCredentialStatus: vi.fn(),
  getAgentGitHub: vi.fn(),
  startGoogleMint: vi.fn(),
  getAgentMailbox: vi.fn(),
  listAgentConnections: vi.fn(),
  role: { current: null as string | null },
}))
vi.mock('@/api/agents', () => ({
  getAgentCredentialStatus: m.getAgentCredentialStatus,
  getAgentGitHub: m.getAgentGitHub,
  setAgentGitHub: vi.fn(),
  checkAgentGitHub: vi.fn(),
  deleteAgentGitHub: vi.fn(),
  startGoogleMint: m.startGoogleMint,
  getGoogleMint: vi.fn(),
  listAgents: vi.fn().mockResolvedValue({ items: [] }),
  listProjects: vi.fn().mockResolvedValue([]),
}))
vi.mock('@/api/mail', () => ({ getAgentMailbox: m.getAgentMailbox }))
vi.mock('@/api/connections', async (orig) => ({
  ...(await orig<typeof import('@/api/connections')>()),
  listAgentConnections: m.listAgentConnections,
}))
vi.mock('@/workspace/WorkspaceProvider', () => ({ useWorkspaceRole: () => m.role.current }))

const { AgentAccountsPanel } = await import('./AgentAccountsPanel')

const GITHUB = {
  repo: 'dimagi-internal/eva', owner_email: 'jj@dimagi.com', lent_by_email: 'jj@dimagi.com', can_lend: true,
  create_url: 'https://github.com/settings/personal-access-tokens/new?name=canopy+eva',
  set: true, login: 'jjackson', expired: false, expiring_soon: false, checks: [], error: '',
  expires_at: '2027-02-01T10:00:00Z', in_vault: true, machines: ['m1'], eligible_machines: ['m1'],
}
const provider = (name: string, title: string, over: Record<string, unknown> = {}) =>
  ({ name, title, workspace: 'w', workspace_name: 'W', configured: true, ...over })
const conn = (over: Record<string, unknown> = {}) => ({
  id: 3, provider: 'salesforce', title: 'Salesforce', account_label: 'eva@dimagi-ai.com', workspace: 'w',
  workspace_name: 'W', instance_url: '', needs_reconnect: false, usable: true, missing_scopes: [],
  connected_at: '2026-10-01T00:00:00Z', last_used_at: null, connected_by_email: 'jj@dimagi.com',
  granted_from: null, shared_with: [], state: 'active', ...over,
})
const GOG = { name: 'gog-token', declared: true, set: true, source: 'canopy', updated_at: null }

function show(opts: { isAdmin?: boolean; email?: string | null } = {}) {
  const { isAdmin = true, email = 'eva@dimagi-ai.com' } = opts
  return render(
    <AuthContext.Provider value={{ status: 'authenticated', user: { email: 'jj@dimagi.com' } as never }}>
      <MemoryRouter initialEntries={['/agents/eva/settings/accounts']}>
        <AgentAccountsPanel agent={{ slug: 'eva', email, workspace: 'w' }} isAdmin={isAdmin} />
      </MemoryRouter>
    </AuthContext.Provider>,
  )
}
const names = () => screen.getAllByRole('listitem').map((r) => r.firstElementChild!.firstElementChild!.textContent)

beforeEach(() => {
  vi.clearAllMocks()
  m.role.current = null
  m.getAgentCredentialStatus.mockResolvedValue([GOG])
  m.getAgentGitHub.mockResolvedValue(GITHUB)
  m.getAgentMailbox.mockRejectedValue(new Error('none'))
  m.listAgentConnections.mockResolvedValue({
    providers: [provider('google', 'Google'), provider('salesforce', 'Salesforce')],
    connections: [],
  })
})
afterEach(cleanup)

describe('External accounts list', () => {
  it('one row per account', async () => {
    show()
    await screen.findByText('Salesforce')
    expect(names()).toEqual(['GitHub', 'Google', 'Salesforce'])
    // every row: a pill and one action
    for (const row of screen.getAllByRole('listitem')) {
      expect(row.querySelector('[data-slot="status-badge"]')).toBeTruthy()
      expect(within(row).getAllByRole('button').length).toBe(1)
    }
    expect(screen.queryByText(/delegated credential/i)).toBeNull()
    expect(document.querySelectorAll('table').length).toBe(0)
    expect(screen.getByTestId('github-status').textContent).toContain('acts as @jjackson')
    expect(screen.getByTestId('github-status').textContent).toContain('expires')
  })

  it('only one Google row when a mailbox is declared', async () => {
    show()
    await screen.findByText('Salesforce')
    expect(names().filter((n) => n?.startsWith('Google'))).toEqual(['Google'])
    expect(screen.getByTestId('credential-status-mailbox').textContent).toContain('eva@dimagi-ai.com')
  })

  it('a Connections Google connection already made is still listed', async () => {
    m.listAgentConnections.mockResolvedValue({
      providers: [provider('google', 'Google'), provider('salesforce', 'Salesforce')],
      connections: [conn({ id: 9, provider: 'google', title: 'Google', account_label: 'other@dimagi.com' })],
    })
    show()
    await screen.findByText('Google (other account)')
    expect(names()).toEqual(['GitHub', 'Google', 'Google (other account)', 'Salesforce'])
    expect(within(screen.getByTestId('agent-connection-9')).getByRole('button', { name: 'Disconnect' })).toBeTruthy()
  })

  it('Connections Google shows when no mailbox', async () => {
    m.getAgentCredentialStatus.mockResolvedValue([])
    show({ email: null })
    await screen.findByText('Salesforce')
    expect(names()).toEqual(['GitHub', 'Google', 'Salesforce'])
    expect(screen.queryByTestId('credential-status-mailbox')).toBeNull()
    expect(within(screen.getByTestId('agent-connection-provider-google')).getByRole('button', { name: 'Connect Google' })).toBeTruthy()
  })

  it('connected Google says Reconnect, not Connect', async () => {
    show()
    const row = await screen.findByTestId('credential-status-mailbox')
    expect(within(row).getByRole('button', { name: 'Reconnect' })).toBeTruthy()
    expect(within(row).queryByRole('button', { name: 'Connect Google' })).toBeNull()
    expect(row.textContent).toContain('Connected')
    fireEvent.click(within(row).getByRole('button', { name: 'Reconnect' }))
    await waitFor(() => expect(m.startGoogleMint).toHaveBeenCalledWith('eva'))
  })

  it('an unconnected mailbox says Not connected and offers Connect Google', async () => {
    m.getAgentCredentialStatus.mockResolvedValue([])
    show()
    const row = await screen.findByTestId('credential-status-mailbox')
    expect(row.textContent).toContain('Not connected')
    expect(within(row).getByRole('button', { name: 'Connect Google' })).toBeTruthy()
  })

  it('GitHub explanation is gone and Replace token opens the link and paste box', async () => {
    show()
    const row = await screen.findByTestId('agent-github')
    expect(row.textContent).not.toMatch(/delegated|sealed|Nobody can read/i)
    expect(row.textContent).not.toMatch(/Resource owner|extract/)
    expect(screen.queryByLabelText('GitHub token')).toBeNull()
    fireEvent.click(within(row).getByRole('button', { name: 'Replace token' }))
    const link = within(row).getByRole('link', { name: 'Create the token on GitHub' })
    expect(link.getAttribute('href')).toBe(GITHUB.create_url)
    expect(link.getAttribute('target')).toBe('_blank')
    expect(row.textContent).toContain('On that page, pick dimagi-internal/eva and any other repo it ships to, then generate it and paste it here.')
    expect(row.textContent).toContain('Check that Resource owner reads dimagi-internal.')
    expect(row.textContent).toContain('Whoever runs a machine that uses this token could extract it.')
    expect(within(row).getByLabelText('GitHub token')).toBeTruthy()
    expect(within(row).getByRole('button', { name: 'Save' })).toBeTruthy()
    expect(within(row).getByRole('button', { name: 'Withdraw' })).toBeTruthy()
  })

  it('an unset GitHub says Connect GitHub, and nobody who cannot delegate gets an action', async () => {
    m.getAgentGitHub.mockResolvedValue({ ...GITHUB, set: false, login: '' })
    show()
    expect(await screen.findByRole('button', { name: 'Connect GitHub' })).toBeTruthy()
    cleanup()
    m.getAgentGitHub.mockResolvedValue({ ...GITHUB, set: false, login: '', can_lend: false })
    show()
    await screen.findByText(/can delegate their GitHub identity/)
    expect(within(screen.getByTestId('agent-github')).queryByRole('button')).toBeNull()
  })

  it('not set up shows a link, not a disabled button', async () => {
    m.role.current = 'admin'
    m.listAgentConnections.mockResolvedValue({
      providers: [provider('google', 'Google'), provider('salesforce', 'Salesforce', { configured: false })],
      connections: [],
    })
    show()
    const row = await screen.findByTestId('agent-connection-provider-salesforce')
    expect(row.textContent).toContain('Not set up')
    expect(within(row).getByRole('link', { name: 'Set it up' }).getAttribute('href')).toBe('/w/w/settings/connections')
    expect(within(row).queryByRole('button')).toBeNull()
  })

  it('non-admin sees no Connections rows', async () => {
    show({ isAdmin: false })
    await screen.findByTestId('credential-status-mailbox')
    expect(names()).toEqual(['GitHub', 'Google'])
    expect(m.listAgentConnections).not.toHaveBeenCalled()
  })

  it('an unconnected Salesforce is a neutral pill with a primary Connect button', async () => {
    show()
    const row = await screen.findByTestId('agent-connection-provider-salesforce')
    expect(row.querySelector('[data-slot="status-badge"]')!.getAttribute('data-tone')).toBe('neutral')
    expect(within(row).getByRole('button', { name: 'Connect Salesforce' })).toBeTruthy()
  })
})
