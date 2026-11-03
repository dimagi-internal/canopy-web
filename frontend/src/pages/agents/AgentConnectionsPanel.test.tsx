// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const m = vi.hoisted(() => ({
  listAgentConnections: vi.fn(),
  disconnectAgentConnection: vi.fn(),
  listAgents: vi.fn(),
  role: { current: null as string | null },
}))
vi.mock('@/api/connections', async (orig) => ({
  ...(await orig<typeof import('@/api/connections')>()),
  listAgentConnections: m.listAgentConnections,
  disconnectAgentConnection: m.disconnectAgentConnection,
}))
vi.mock('@/api/agents', () => ({ listAgents: m.listAgents }))
vi.mock('@/workspace/WorkspaceProvider', () => ({ useWorkspaceRole: () => m.role.current }))

import { AgentConnectionsPanel } from './AgentConnectionsPanel'

const PROVIDER = { name: 'salesforce', title: 'Salesforce', workspace: 'w', workspace_name: 'W', configured: true }
function conn(over: Record<string, unknown> = {}) {
  return {
    id: 3, provider: 'salesforce', title: 'Salesforce', account_label: 'eva@dimagi-ai.com', workspace: 'w', workspace_name: 'W',
    instance_url: 'https://dimagi.my.salesforce.com', needs_reconnect: false, usable: true, missing_scopes: [],
    connected_at: '2026-10-01T00:00:00Z', last_used_at: null, connected_by_email: 'jj@dimagi.com',
    granted_from: null, shared_with: [], state: 'active',
    statement: { version: 1, agents: [], my_computers: [], endorsers: [] }, holders: [], waiting: [], my_machines: [], ...over,
  }
}
const show = () =>
  render(<MemoryRouter initialEntries={['/agents/eva/settings/accounts']}><AgentConnectionsPanel agent={{ slug: 'eva' }} /></MemoryRouter>)

beforeEach(() => {
  vi.clearAllMocks()
  m.role.current = null
  m.listAgents.mockResolvedValue({ items: [{ slug: 'eva' }, { slug: 'echo' }], total: 2, offset: 0, limit: 200 })
  m.disconnectAgentConnection.mockResolvedValue(undefined)
})
afterEach(cleanup)

describe('AgentConnectionsPanel', () => {
  it('connect is a CSRF-carrying form POST with return_to and the agent, never a link', async () => {
    document.cookie = 'csrftoken=tok123'
    m.listAgentConnections.mockResolvedValue({ providers: [PROVIDER], connections: [] })
    show()
    const btn = await screen.findByRole('button', { name: 'Connect Salesforce' })
    const form = btn.closest('form') as HTMLFormElement
    expect(form.getAttribute('method')).toBe('post')
    expect(form.getAttribute('action')).toMatch(/\/auth\/connections\/salesforce\/w\/start\/$/)
    const field = (n: string) => (form.querySelector(`input[name="${n}"]`) as HTMLInputElement).value
    expect(field('return_to')).toBe('/agents/eva/settings/accounts')
    expect(field('agent')).toBe('eva')
    expect(field('csrfmiddlewaretoken')).toBe('tok123')
    expect(screen.queryByRole('link', { name: 'Connect Salesforce' })).toBeNull()
  })

  it('shows what the grant allows, own and shared', async () => {
    m.listAgentConnections.mockResolvedValue({
      providers: [PROVIDER],
      connections: [conn({ scopes: ['api', 'refresh_token'] }), conn({ id: 9, granted_from: 'hal', scopes: ['api'] })],
    })
    show()
    expect(await screen.findByText('Scopes: api, refresh_token')).toBeTruthy()
    expect(screen.getByText('Scopes: api')).toBeTruthy()
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
    expect((await screen.findByRole('button', { name: 'Reconnect' })).closest('form')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Connect Other' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('Not set up for this workspace')).toBeTruthy()
    // Not an admin of the workspace: no link to a settings page they cannot use.
    expect(screen.queryByRole('link', { name: 'Set it up' })).toBeNull()
  })

  it('starts the connect under the agent\'s workspace', async () => {
    m.listAgentConnections.mockResolvedValue({ providers: [{ ...PROVIDER, workspace: 'dimagi' }], connections: [] })
    show()
    const form = (await screen.findByRole('button', { name: 'Connect Salesforce' })).closest('form') as HTMLFormElement
    expect(form.getAttribute('action')).toMatch(/\/auth\/connections\/salesforce\/dimagi\/start\/$/)
  })

  it('links a workspace admin to where the provider is set up', async () => {
    m.role.current = 'admin'
    m.listAgentConnections.mockResolvedValue({
      providers: [{ name: 'google', title: 'Google', workspace: 'dimagi', workspace_name: 'Dimagi', configured: false }],
      connections: [],
    })
    show()
    const link = await screen.findByRole('link', { name: 'Set it up' })
    expect(link.getAttribute('href')).toBe('/w/dimagi/settings/connections')
  })

  it('an unshared agent connection says so and Share goes to the share page', async () => {
    m.listAgentConnections.mockResolvedValue({ providers: [PROVIDER], connections: [conn({ state: 'unshared' })] })
    show()
    expect(await screen.findByText('Not shared yet: no agent or computer can use it.')).toBeTruthy()
    const a = screen.getByRole('link', { name: 'Share' }) as HTMLAnchorElement
    expect(a.getAttribute('href')).toBe('/w/w/connections/3/share?return_to=%2Fagents%2Feva%2Fsettings%2Faccounts')
    expect(screen.queryByLabelText('Share with')).toBeNull()
  })

  it('shows who it is lent to read-only, with one link to change it and waiting machines named', async () => {
    m.listAgentConnections.mockResolvedValue({
      providers: [PROVIDER],
      connections: [conn({
        shared_with: ['echo'], statement: { version: 2, agents: ['echo'], my_computers: [], endorsers: [] },
        waiting: [{ agent: 'echo', runner: 'echo box' }],
      })],
    })
    show()
    expect(await screen.findByText('Lent to: echo')).toBeTruthy()
    expect(screen.getByText('Waiting for a machine that holds it: echo box (echo)')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /Stop sharing/ })).toBeNull()
    expect(screen.getByRole('link', { name: 'Change who can use it' })).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Give waiting machines a copy' })).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Open to check its machines' })).toBeTruthy()
  })

  it('shows a connection lent by another agent as lent, with nothing to change', async () => {
    m.listAgentConnections.mockResolvedValue({
      providers: [PROVIDER], connections: [conn({ id: 9, granted_from: 'hal' })],
    })
    show()
    expect(await screen.findByText(/Lent by hal/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Disconnect' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Stop using' })).toBeNull()
    expect(screen.queryByRole('link', { name: /Share|Change who/ })).toBeNull()
  })

  it('says Reconnect required for an agent’s own expired connection', async () => {
    m.listAgentConnections.mockResolvedValue({ providers: [PROVIDER], connections: [conn({ needs_reconnect: true })] })
    show()
    expect(await screen.findByText('Reconnect required.')).toBeTruthy()
    expect(screen.queryByRole('link', { name: 'Change who can use it' })).toBeNull()
  })

  it('never lists a connection that is being disconnected', async () => {
    m.listAgentConnections.mockResolvedValue({
      providers: [PROVIDER], connections: [conn({ state: 'revoking', account_label: 'gone@x.test' })],
    })
    show()
    await screen.findByTestId('agent-connection-provider-salesforce')
    expect(screen.queryByText('gone@x.test')).toBeNull()
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

  it('names the wrong_account refusal (an admin signed in as themselves for the agent)', async () => {
    m.listAgentConnections.mockResolvedValue({ providers: [PROVIDER], connections: [] })
    render(
      <MemoryRouter initialEntries={['/x?connection=salesforce&status=error&reason=wrong_account']}>
        <AgentConnectionsPanel agent={{ slug: 'eva' }} />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/^Salesforce was signed in with an account this connection may not use — sign in as the agent’s own account/)).toBeTruthy()
    expect(screen.queryByText(/failed\. Try again/)).toBeNull()
  })

  it('reconnects to the connection’s own workspace, not the provider row’s', async () => {
    document.cookie = 'csrftoken=tok123'
    m.listAgentConnections.mockResolvedValue({
      providers: [PROVIDER],
      connections: [conn({ workspace: 'w', needs_reconnect: true })],
    })
    render(
      <MemoryRouter initialEntries={['/a']}>
        <AgentConnectionsPanel agent={{ slug: 'eva', workspace: 'elsewhere' }} />
      </MemoryRouter>,
    )
    const btn = await screen.findByRole('button', { name: 'Reconnect' })
    expect((btn.closest('form') as HTMLFormElement).getAttribute('action')).toMatch(/\/auth\/connections\/salesforce\/w\/start\/$/)
  })

  it('shows a connection made before a move with Disconnect only, and offers Connect here', async () => {
    m.listAgentConnections.mockResolvedValue({
      providers: [{ ...PROVIDER, workspace: 'x', workspace_name: 'X' }],
      connections: [conn({ usable: false, needs_reconnect: true, workspace: 'dimagi', workspace_name: 'Dimagi', shared_with: ['echo'] })],
    })
    show()
    expect(await screen.findByText('Made in Dimagi before the move — disconnect it and connect here.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Reconnect' })).toBeNull()
    expect(screen.queryByRole('link', { name: 'Share' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeTruthy()
    const connect = screen.getByRole('button', { name: 'Connect Salesforce' })
    expect((connect.closest('form') as HTMLFormElement).getAttribute('action')).toMatch(/\/auth\/connections\/salesforce\/x\/start\/$/)
  })

  it('says when a shared connection stopped because its owner moved', async () => {
    m.listAgentConnections.mockResolvedValue({
      providers: [PROVIDER],
      connections: [conn({ id: 9, granted_from: 'hal', usable: false, workspace: 'dimagi', workspace_name: 'Dimagi' })],
    })
    show()
    expect(await screen.findByText('hal is no longer in Dimagi, where this was made, so it no longer works.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Stop using' })).toBeNull()
  })

  it('prompts to reconnect for a scope the app added since', async () => {
    m.listAgentConnections.mockResolvedValue({
      providers: [PROVIDER],
      connections: [
        conn({ missing_scopes: [{ scope: 's', label: 'Send mail' }] }),
        conn({ id: 9, granted_from: 'hal', missing_scopes: [{ scope: 's', label: 'Send mail' }] }),
      ],
    })
    show()
    expect(await screen.findByText('Reconnect to allow: Send mail')).toBeTruthy()
    expect(screen.getByText('Its owner must reconnect to allow: Send mail')).toBeTruthy()
    expect(screen.getAllByRole('button', { name: 'Reconnect' })).toHaveLength(1)
  })
})
