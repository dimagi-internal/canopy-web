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

const PROVIDER = { name: 'salesforce', title: 'Salesforce', configured: true }
function conn(over: Record<string, unknown> = {}) {
  return {
    id: 7, provider: 'salesforce', title: 'Salesforce', account_label: 'jj@dimagi.com',
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
    expect(a.getAttribute('href')).toContain('/auth/connections/salesforce/start/?return_to=%2Fsettings%2Fconnections')
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
    expect(await screen.findByText(/Connecting salesforce failed \(access_denied\)/)).toBeTruthy()
  })
})
