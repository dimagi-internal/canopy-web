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
    instance_url: 'https://dimagi.my.salesforce.com', needs_reconnect: false,
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
    expect(screen.getByText('Not set up on this deployment')).toBeTruthy()
  })

  it('reports the callback result', async () => {
    listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [] })
    at('/settings?connection=salesforce&status=error&reason=access_denied')
    expect(await screen.findByText(/Salesforce was not approved/)).toBeTruthy()
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

  it('lists a provider once per workspace that set it up, each connecting with its own app', async () => {
    const other = { ...PROVIDER, workspace: 'b', workspace_name: 'B' }
    listMyConnections.mockResolvedValue({
      providers: [PROVIDER, other],
      connections: [conn({ id: 1, workspace: 'b', account_label: 'in-b@dimagi.com' }),
        conn({ id: 2, workspace: 'gone', account_label: 'orphan@dimagi.com' })],
    })
    at('/settings')
    const rows = await screen.findAllByTestId('connection-provider-salesforce')
    expect(rows).toHaveLength(2)
    expect(rows[0].textContent).toContain('orphan@dimagi.com')      // a workspace with no row: first row
    expect(rows[0].textContent).not.toContain('in-b@dimagi.com')
    expect(rows[1].textContent).toContain('in-b@dimagi.com')
  })

  it('connects each workspace with its own start URL', async () => {
    listMyConnections.mockResolvedValue({
      providers: [PROVIDER, { ...PROVIDER, workspace: 'b', workspace_name: 'B' }], connections: [],
    })
    at('/settings')
    await screen.findAllByTestId('connection-provider-salesforce')
    const hrefs = screen.getAllByRole('link', { name: 'Connect Salesforce' }).map((a) => a.getAttribute('href'))
    expect(hrefs).toHaveLength(2)
    expect(hrefs[0]).toContain('/auth/connections/salesforce/w/start/')
    expect(hrefs[1]).toContain('/auth/connections/salesforce/b/start/')
  })

  it('says what an insufficient_scope connect means', async () => {
    listMyConnections.mockResolvedValue({ providers: [PROVIDER], connections: [] })
    at('/settings?connection=salesforce&status=error&reason=insufficient_scope')
    expect(await screen.findByText(/Salesforce was connected without every permission canopy needs/)).toBeTruthy()
  })
})
