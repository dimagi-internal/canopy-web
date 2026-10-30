// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const { listMyConnections, disconnectMine } = vi.hoisted(() => ({
  listMyConnections: vi.fn(),
  disconnectMine: vi.fn(),
}))
vi.mock('@/api/connections', async (orig) => ({
  ...(await orig<typeof import('@/api/connections')>()),
  listMyConnections,
  disconnectMine,
}))

import { ConnectionsPanel } from './ConnectionsPanel'

const PROVIDER = { name: 'salesforce', title: 'Salesforce', workspace: 'w', workspace_name: 'W', configured: true }
function conn(over: Record<string, unknown> = {}) {
  return {
    id: 7, provider: 'salesforce', title: 'Salesforce', account_label: 'jj@dimagi.com', workspace: 'w', workspace_name: 'W',
    instance_url: 'https://dimagi.my.salesforce.com', needs_reconnect: false, usable: true, missing_scopes: [],
    connected_at: '2026-10-01T00:00:00Z', last_used_at: null, connected_by_email: 'jj@dimagi.com',
    granted_from: null, ...over,
  }
}
const at = (url: string) =>
  render(<MemoryRouter initialEntries={[url]}><ConnectionsPanel /></MemoryRouter>)

beforeEach(() => vi.clearAllMocks())
afterEach(cleanup)

describe('ConnectionsPanel', () => {
  it('offers Connect, carrying return_to, when nothing is connected', async () => {
    listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [] })
    at('/settings/connections')
    const a = (await screen.findByRole('link', { name: 'Connect Salesforce' })) as HTMLAnchorElement
    expect(a.getAttribute('href')).toContain('/auth/connections/salesforce/w/start/?return_to=%2Fsettings%2Fconnections')
    expect(a.getAttribute('href')).not.toContain('agent=')
  })

  it('shows account and instance when connected, and disconnect calls DELETE', async () => {
    listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [conn()] })
    disconnectMine.mockResolvedValue(undefined)
    at('/settings')
    expect(await screen.findByText('jj@dimagi.com')).toBeTruthy()
    expect(screen.getByText(/dimagi\.my\.salesforce\.com/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
    await waitFor(() => expect(disconnectMine).toHaveBeenCalledWith(7))
  })

  it('shows what the grant allows', async () => {
    listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [conn({ scopes: ['api', 'refresh_token'] })] })
    at('/settings')
    expect(await screen.findByText('Scopes: api, refresh_token')).toBeTruthy()
  })

  it('offers Reconnect when the grant needs it', async () => {
    listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [conn({ needs_reconnect: true })] })
    at('/settings')
    expect(await screen.findByRole('link', { name: 'Reconnect' })).toBeTruthy()
  })

  it('disables Connect when the provider is not configured', async () => {
    listMyConnections.mockResolvedValue({ providers: [{ ...PROVIDER, configured: false }], connections: [] })
    at('/settings')
    const b = (await screen.findByRole('button', { name: 'Connect Salesforce' })) as HTMLButtonElement
    expect(b.disabled).toBe(true)
    expect(screen.getByText('Not set up in your workspaces')).toBeTruthy()
    expect(document.body.textContent).not.toMatch(/deployment/)
  })

  it('reports the callback result', async () => {
    listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [] })
    at('/settings?connection=salesforce&status=error&reason=access_denied')
    expect(await screen.findByText(/Salesforce was not approved/)).toBeTruthy()
  })

  it('names the wrong_account refusal instead of a generic failure', async () => {
    listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [] })
    at('/settings?connection=salesforce&status=error&reason=wrong_account')
    expect(await screen.findByText(
      'Salesforce was signed in with an account this connection may not use — sign in as the agent’s own account, or one in the workspace’s allowed domains.',
    )).toBeTruthy()
    expect(document.body.textContent).not.toMatch(/failed\. Try again/)
  })

  it('words forbidden neutrally: it is also what someone who left the workspace sees', async () => {
    listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [] })
    at('/settings?connection=salesforce&status=error&reason=forbidden')
    expect(await screen.findByText('Salesforce is no longer allowed for this account or workspace.')).toBeTruthy()
  })

  it('shows a connection in a workspace I left as not working, with Disconnect only', async () => {
    listMyConnections.mockResolvedValue({
      providers: [PROVIDER],
      connections: [conn({ usable: false, needs_reconnect: true, workspace: 'old', workspace_name: 'Old Co' })],
    })
    at('/settings')
    expect(await screen.findByText(/You are no longer in Old Co/)).toBeTruthy()
    expect(screen.queryByRole('link', { name: 'Reconnect' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeTruthy()
  })

  it('prompts to reconnect for a scope the app added since', async () => {
    listMyConnections.mockResolvedValue({
      providers: [PROVIDER],
      connections: [conn({ missing_scopes: [{ scope: 'https://www.googleapis.com/auth/gmail.send', label: 'Send mail' }] })],
    })
    at('/settings')
    expect(await screen.findByText('Reconnect to allow: Send mail')).toBeTruthy()
    const a = screen.getByRole('link', { name: 'Reconnect' }) as HTMLAnchorElement
    expect(a.getAttribute('href')).toContain('/auth/connections/salesforce/w/start/')
  })

  it('renders whatever providers the API lists, with no API host for a fixed-host one', async () => {
    const gmail = { name: 'gmail', title: 'Gmail', workspace: 'w', workspace_name: 'W', configured: true }
    listMyConnections.mockResolvedValue({
      providers: [gmail, PROVIDER],
      connections: [conn({ id: 9, provider: 'gmail', title: 'Gmail', instance_url: '', account_label: 'jj@gmail.test' })],
    })
    at('/settings')
    expect(await screen.findByTestId('connection-provider-gmail')).toBeTruthy()
    expect(screen.getByText('jj@gmail.test')).toBeTruthy()
    expect(screen.getByText('Not used yet').textContent).toBe('Not used yet')   // no leading " · "
    const a = screen.getByRole('link', { name: 'Connect Salesforce' }) as HTMLAnchorElement
    expect(a.getAttribute('href')).toContain('/auth/connections/salesforce/w/start/')
  })

  it('one Connect per workspace that set the provider up, each naming its workspace and using its start URL', async () => {
    listMyConnections.mockResolvedValue({
      providers: [PROVIDER, { ...PROVIDER, workspace: 'b', workspace_name: 'B' }], connections: [],
    })
    at('/settings')
    expect(await screen.findAllByTestId('connection-provider-salesforce')).toHaveLength(1)
    const w = screen.getByRole('link', { name: 'Connect Salesforce in W' }) as HTMLAnchorElement
    const b = screen.getByRole('link', { name: 'Connect Salesforce in B' }) as HTMLAnchorElement
    expect(w.getAttribute('href')).toContain('/auth/connections/salesforce/w/start/')
    expect(b.getAttribute('href')).toContain('/auth/connections/salesforce/b/start/')
  })

  it('leaves the workspace out of Connect when only one of the person\'s workspaces set the provider up', async () => {
    listMyConnections.mockResolvedValue({
      providers: [PROVIDER, { name: 'google', title: 'Google', workspace: 'b', workspace_name: 'B', configured: true }],
      connections: [],
    })
    at('/settings')
    expect(await screen.findByRole('link', { name: 'Connect Salesforce' })).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Connect Google' })).toBeTruthy()
    expect(screen.queryByText(/ in [WB]$/)).toBeNull()
  })

  it('a workspace already connected in shows the connection, and Connect only for the other', async () => {
    listMyConnections.mockResolvedValue({
      providers: [PROVIDER, { ...PROVIDER, workspace: 'b', workspace_name: 'B' }],
      connections: [conn({ id: 1, workspace: 'w', workspace_name: 'W' })],
    })
    at('/settings')
    expect(await screen.findByRole('link', { name: 'Connect Salesforce in B' })).toBeTruthy()
    expect(screen.queryByRole('link', { name: 'Connect Salesforce in W' })).toBeNull()
  })

  it('names each connection\'s workspace only when the person has connections in more than one', async () => {
    listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [conn()] })
    at('/settings')
    await screen.findByText('jj@dimagi.com')
    expect(screen.queryByText(/^In W/)).toBeNull()
    cleanup()

    listMyConnections.mockResolvedValue({
      providers: [PROVIDER, { ...PROVIDER, workspace: 'b', workspace_name: 'B' }],
      connections: [conn({ id: 1, account_label: 'a@dimagi.com' }),
        conn({ id: 2, workspace: 'b', workspace_name: 'B', account_label: 'b@dimagi.com', needs_reconnect: true })],
    })
    at('/settings')
    await screen.findByText('b@dimagi.com')
    expect(screen.getByText(/^In W · /)).toBeTruthy()
    expect(screen.getByText(/^In B · /)).toBeTruthy()
    // Reconnect goes to the connection's own workspace.
    expect(screen.getByRole('link', { name: 'Reconnect' }).getAttribute('href'))
      .toContain('/auth/connections/salesforce/b/start/')
  })

  it('says what an insufficient_scope connect means', async () => {
    listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [] })
    at('/settings?connection=salesforce&status=error&reason=insufficient_scope')
    expect(await screen.findByText(/Salesforce was connected without every permission canopy needs/)).toBeTruthy()
  })
})
