// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const m = vi.hoisted(() => ({
  listAgentConnections: vi.fn(),
  disconnectAgentConnection: vi.fn(),
  grantConnection: vi.fn(),
  revokeConnectionGrant: vi.fn(),
  listAgents: vi.fn(),
}))
vi.mock('@/api/connections', async (orig) => ({ ...(await orig<typeof import('@/api/connections')>()), ...m }))
vi.mock('@/api/agents', () => ({ listAgents: m.listAgents }))

import { AgentConnectionsPanel } from './AgentConnectionsPanel'

const PROVIDER = { name: 'salesforce', title: 'Salesforce', configured: true }
function conn(over: Record<string, unknown> = {}) {
  return {
    id: 3, provider: 'salesforce', title: 'Salesforce', account_label: 'eva@dimagi-ai.com',
    instance_url: 'https://dimagi.my.salesforce.com', needs_reconnect: false,
    connected_at: '2026-10-01T00:00:00Z', last_used_at: null, connected_by_email: 'jj@dimagi.com',
    granted_from: null, shared_with: [], ...over,
  }
}
const show = () =>
  render(<MemoryRouter initialEntries={['/agents/eva/settings/accounts']}><AgentConnectionsPanel agent={{ slug: 'eva' }} /></MemoryRouter>)

beforeEach(() => {
  vi.clearAllMocks()
  m.listAgents.mockResolvedValue({ items: [{ slug: 'eva' }, { slug: 'echo' }], total: 2, offset: 0, limit: 200 })
  for (const k of ['disconnectAgentConnection', 'grantConnection', 'revokeConnectionGrant'] as const) m[k].mockResolvedValue(undefined)
})
afterEach(cleanup)

describe('AgentConnectionsPanel', () => {
  it('connect link carries return_to and the agent', async () => {
    m.listAgentConnections.mockResolvedValue({ providers: [PROVIDER], connections: [] })
    show()
    const a = (await screen.findByRole('link', { name: 'Connect Salesforce' })) as HTMLAnchorElement
    expect(a.getAttribute('href')).toContain('return_to=%2Fagents%2Feva%2Fsettings%2Faccounts')
    expect(a.getAttribute('href')).toContain('agent=eva')
  })

  it('disconnects an own connection', async () => {
    m.listAgentConnections.mockResolvedValue({ providers: [PROVIDER], connections: [conn()] })
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }))
    await waitFor(() => expect(m.disconnectAgentConnection).toHaveBeenCalledWith('eva', 3))
  })

  it('shows Reconnect, and a not-configured provider disabled', async () => {
    m.listAgentConnections.mockResolvedValue({
      providers: [PROVIDER, { name: 'other', title: 'Other', configured: false }],
      connections: [conn({ needs_reconnect: true })],
    })
    show()
    expect(await screen.findByRole('link', { name: 'Reconnect' })).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Connect Other' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('shares with another agent (chip appears, picker clears), then stops sharing (chip goes)', async () => {
    let sharedWith: string[] = []
    m.listAgentConnections.mockImplementation(async () => ({
      providers: [PROVIDER], connections: [conn({ shared_with: sharedWith })],
    }))
    m.grantConnection.mockImplementation(async (_s: string, _i: number, to: string) => { sharedWith = [to] })
    m.revokeConnectionGrant.mockImplementation(async () => { sharedWith = [] })
    show()
    const sel = (await screen.findByLabelText('Share with')) as HTMLSelectElement
    await screen.findByRole('option', { name: 'echo' })
    // The agent itself is never offered.
    expect(screen.queryByRole('option', { name: 'eva' })).toBeNull()
    fireEvent.change(sel, { target: { value: 'echo' } })
    fireEvent.click(screen.getByRole('button', { name: 'Share' }))
    await waitFor(() => expect(m.grantConnection).toHaveBeenCalledWith('eva', 3, 'echo'))
    const stop = await screen.findByRole('button', { name: 'Stop sharing with echo' })
    expect(screen.getByText('Shared with echo.')).toBeTruthy()
    // Already shared: not offered again, so the picker has nothing left.
    expect(screen.queryByLabelText('Share with')).toBeNull()
    fireEvent.click(stop)
    await waitFor(() => expect(m.revokeConnectionGrant).toHaveBeenCalledWith('eva', 3, 'echo'))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Stop sharing with echo' })).toBeNull())
    expect(((await screen.findByLabelText('Share with')) as HTMLSelectElement).value).toBe('')
  })

  it('shows a connection shared by another agent and lets the admin stop using it', async () => {
    m.listAgentConnections.mockResolvedValue({
      providers: [PROVIDER], connections: [conn({ id: 9, granted_from: 'hal' })],
    })
    show()
    expect(await screen.findByText(/shared by hal/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Disconnect' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Stop using' }))
    await waitFor(() => expect(m.revokeConnectionGrant).toHaveBeenCalledWith('hal', 9, 'eva'))
  })

  it('never prints query-string text it does not know', async () => {
    m.listAgentConnections.mockResolvedValue({ providers: [PROVIDER], connections: [] })
    render(
      <MemoryRouter initialEntries={['/x?connection=%3Cb%3Eevil&status=error&reason=Send%20money']}>
        <AgentConnectionsPanel agent={{ slug: 'eva' }} />
      </MemoryRouter>,
    )
    expect(await screen.findByText('Connecting the service failed. Try again.')).toBeTruthy()
    expect(screen.queryByText(/evil|money/)).toBeNull()
  })
})
