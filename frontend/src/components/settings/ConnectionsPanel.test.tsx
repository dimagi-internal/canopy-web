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
    granted_from: null, state: 'active', statement: { version: 1, agents: [], my_computers: [], endorsers: [] },
    holders: [], waiting: [], my_machines: [], ...over,
  }
}
const at = (url: string) =>
  render(<MemoryRouter initialEntries={[url]}><ConnectionsPanel /></MemoryRouter>)

beforeEach(() => { vi.clearAllMocks(); disconnectMine.mockResolvedValue('revoking') })
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
    disconnectMine.mockResolvedValue('revoking')
    at('/settings')
    expect(await screen.findByText('jj@dimagi.com')).toBeTruthy()
    expect(screen.getByText(/dimagi\.my\.salesforce\.com/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
    await waitFor(() => expect(disconnectMine).toHaveBeenCalledWith(7))
    expect(await screen.findByText('Disconnected. A machine will revoke it shortly.')).toBeTruthy()
  })

  it('warns when a connection is sealed to a key its owner replaced, and says to revoke it by hand if nothing will', async () => {
    listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [conn({ owner_key_replaced: true })] })
    disconnectMine.mockResolvedValue('deleted')
    at('/settings')
    expect(await screen.findByText('Sealed to a vault key you replaced. New tokens stay readable by the old key until you disconnect and connect again. If it may have leaked, disconnect now.')).toBeTruthy()
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }))
    expect(await screen.findByText('Disconnected. Salesforce or Google may keep the old sign-in alive until it idles out. Revoke it at Salesforce or Google yourself.')).toBeTruthy()
  })

  it('a machine revoking it needs no such note', async () => {
    listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [conn({ owner_key_replaced: true })] })
    at('/settings')
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }))
    expect(await screen.findByText('Disconnected. A machine will revoke it shortly.')).toBeTruthy()
  })

  it('says plainly when nothing will revoke it', async () => {
    listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [conn()] })
    disconnectMine.mockResolvedValue('deleted')
    at('/settings')
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }))
    expect(await screen.findByText('Disconnected. Salesforce or Google may keep the old sign-in alive until it idles out.')).toBeTruthy()
  })

  it('says which lent agent a connection is not reaching, with a link to the share page', async () => {
    listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [conn({
      id: 'c1', statement: { version: 1, agents: ['eva'], my_computers: [], endorsers: [] },
      unreachable: [{ agent: 'eva', reason: 'unvouched' }],
    })] })
    at('/settings')
    const line = (await screen.findByText(/Not reaching eva: no machine is vouched for yet\./))
    expect(line.querySelector('a')?.getAttribute('href')).toContain('/connections/c1/share')
  })

  it('names the owner_changed refusal exactly', async () => {
    listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [] })
    at('/settings?connection=salesforce&status=error&reason=owner_changed')
    expect(await screen.findByText('This account is connected with a vault key that is not yours now. Disconnect it first, then connect again.')).toBeTruthy()
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

  it.each([
    ['provider_error', 'Salesforce or Google refused this sign-in. Check the app’s settings, then try again.'],
    ['no_vault_key', 'Salesforce needs your vault key first. Set up your vault key in Settings, then connect again.'],
    ['revoking', 'Salesforce is being disconnected. Wait for that to finish, then connect again.'],
  ])('names the %s refusal instead of a generic failure', async (reason, sentence) => {
    listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [] })
    at(`/settings?connection=salesforce&status=error&reason=${reason}`)
    expect(await screen.findByText(sentence)).toBeTruthy()
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

  describe('sharing', () => {
    const SHARE = '/w/w/connections/7/share?return_to=%2Fsettings'
    const href = (name: string | RegExp) => (screen.getByRole('link', { name }) as HTMLAnchorElement).getAttribute('href')

    it('says an unshared connection is not shared yet, and Share goes to the share page', async () => {
      listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [conn({ state: 'unshared' })] })
      at('/settings')
      expect(await screen.findByText('Not shared yet: no agent or computer can use it.')).toBeTruthy()
      expect(href('Share')).toBe(SHARE)
    })

    it('shows who it is lent to, the computers it is used on, and who holds it', async () => {
      listMyConnections.mockResolvedValue({
        providers: [PROVIDER],
        connections: [conn({
          statement: { version: 2, agents: ['eva', 'echo'], my_computers: ['j'], endorsers: [] },
          my_machines: [{ jkt: 'j', name: 'Laptop', fingerprint: 'aaaa bbbb' }],
          holders: ['eva box'],
        })],
      })
      at('/settings')
      expect(await screen.findByText('Lent to: eva, echo')).toBeTruthy()
      expect(screen.getByText('Used on your computers: Laptop (aaaa bbbb)')).toBeTruthy()
      expect(screen.getByText('Held by: eva box')).toBeTruthy()
      expect(href('Change who can use it')).toBe(SHARE)
      expect(screen.queryByText(/Not shared yet/)).toBeNull()
    })

    it('names the machines still waiting and sends Give waiting machines a copy to the share page', async () => {
      listMyConnections.mockResolvedValue({
        providers: [PROVIDER],
        connections: [conn({ waiting: [{ agent: 'eva', runner: 'eva box' }, { agent: '', runner: 'Laptop' }] })],
      })
      at('/settings')
      expect(await screen.findByText('Waiting for a machine that holds it: eva box (eva), Laptop')).toBeTruthy()
      expect(href('Give waiting machines a copy')).toBe(SHARE)
    })

    it('always offers a neutral way to check a shared connection’s machines', async () => {
      listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [conn()] })
      at('/settings')
      expect(await screen.findByRole('link', { name: 'Open to check its machines' })).toBeTruthy()
      expect(href('Open to check its machines')).toBe(SHARE)
    })

    it('says Reconnect required and lends nothing to change', async () => {
      listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [conn({ needs_reconnect: true })] })
      at('/settings')
      expect(await screen.findByText('Reconnect required.')).toBeTruthy()
      expect(screen.queryByRole('link', { name: 'Change who can use it' })).toBeNull()
      expect(screen.queryByRole('link', { name: 'Share' })).toBeNull()
      expect(href('Reconnect')).toContain('/auth/connections/salesforce/w/start/')
    })

    it('never lists a connection that is being disconnected', async () => {
      listMyConnections.mockResolvedValue({
        providers: [PROVIDER], connections: [conn({ state: 'revoking', account_label: 'gone@dimagi.com' })],
      })
      at('/settings')
      await screen.findByTestId('connection-provider-salesforce')
      expect(screen.queryByText('gone@dimagi.com')).toBeNull()
    })
  })
})
