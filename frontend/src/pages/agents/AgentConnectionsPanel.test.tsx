// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AuthContext } from '@/auth/AuthProvider'

const m = vi.hoisted(() => ({
  listAgentConnections: vi.fn(),
  disconnectAgentConnection: vi.fn(),
  listAgents: vi.fn(),
  listAgentProjects: vi.fn(),
  listWorkspaceProjects: vi.fn(),
  listInstalledApps: vi.fn(),
  listSurfaceShares: vi.fn(),
  previewSurfaceShare: vi.fn(),
  approveSurfaceShare: vi.fn(),
  stopSurfaceShare: vi.fn(),
  role: { current: null as string | null },
  unlock: vi.fn(),
  reportConnect: vi.fn(),
}))
vi.mock('@/vault/useVaultUnlock', () => ({ useVaultUnlock: () => ({ unlock: m.unlock, element: null }) }))
vi.mock('@/vault/connectReport', async (orig) => ({
  ...(await orig<typeof import('@/vault/connectReport')>()),
  reportConnect: (r: unknown) => m.reportConnect(r),
}))
vi.mock('@/api/connections', async (orig) => ({
  ...(await orig<typeof import('@/api/connections')>()),
  listAgentConnections: m.listAgentConnections,
  disconnectAgentConnection: m.disconnectAgentConnection,
  listSurfaceShares: m.listSurfaceShares,
  previewSurfaceShare: m.previewSurfaceShare,
  approveSurfaceShare: m.approveSurfaceShare,
  stopSurfaceShare: m.stopSurfaceShare,
}))
vi.mock('@/api/agents', () => ({ listAgents: m.listAgents, listProjects: m.listAgentProjects }))
vi.mock('@/api/work', () => ({ listProjects: m.listWorkspaceProjects }))
vi.mock('@/api/workspaceApps', () => ({ listInstalledApps: m.listInstalledApps }))
vi.mock('@/workspace/WorkspaceProvider', () => ({ useWorkspaceRole: () => m.role.current }))

import { ConnectionRows, ConnectionsBanner, useAgentConnections } from './AgentConnectionsPanel'

/** The connection rows as the External accounts tab lays them out (no mailbox row). */
function AgentConnectionsPanel({ agent = { slug: 'eva' } }: { agent?: { slug: string; workspace?: string } }) {
  const conns = useAgentConnections(agent)
  return (
    <>
      <ConnectionsBanner conns={conns} />
      <ConnectionRows agent={agent} conns={conns} mailboxShown={false} />
    </>
  )
}

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
const me = { current: 'jj@dimagi.com' }
const show = () =>
  render(
    <AuthContext.Provider value={{ status: 'authenticated', user: { email: me.current } as never }}>
      <MemoryRouter initialEntries={['/agents/eva/settings/accounts']}><AgentConnectionsPanel /></MemoryRouter>
    </AuthContext.Provider>,
  )

beforeEach(() => {
  vi.clearAllMocks()
  m.role.current = null
  me.current = 'jj@dimagi.com'
  m.listAgents.mockResolvedValue({ items: [{ slug: 'eva' }, { slug: 'echo' }], total: 2, offset: 0, limit: 200 })
  m.disconnectAgentConnection.mockResolvedValue({ outcome: 'revoking', machine_revokes: true })
  m.listSurfaceShares.mockResolvedValue([])
  m.listAgentProjects.mockResolvedValue([])
  m.listWorkspaceProjects.mockResolvedValue([{ id: 7, handle: 'leads', name: 'Lead intake', workspace: 'w' }])
  m.listInstalledApps.mockResolvedValue([{ key: 'crm', name: 'CRM', workspace: 'w' }])
  m.stopSurfaceShare.mockResolvedValue(undefined)
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

  it('the Connect tap opens the vault key first, then goes to the provider', async () => {
    m.unlock.mockResolvedValue({ userId: '7' })
    const submit = vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(() => undefined)
    m.listAgentConnections.mockResolvedValue({ providers: [PROVIDER], connections: [] })
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Connect Salesforce' }))
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1))
    expect(m.unlock).toHaveBeenCalledWith()
    expect(m.reportConnect).toHaveBeenCalledWith(expect.objectContaining({ key: 'salesforce', agent: 'eva', step: 'preflight', outcome: 'ok' }))
    submit.mockRestore()
  })

  it('a vault key that will not open stops the connect before the provider, saying why', async () => {
    m.unlock.mockRejectedValue(new DOMException('no', 'NotAllowedError'))
    const submit = vi.spyOn(HTMLFormElement.prototype, 'submit').mockImplementation(() => undefined)
    m.listAgentConnections.mockResolvedValue({ providers: [PROVIDER], connections: [] })
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Connect Salesforce' }))
    expect((await screen.findByRole('alert')).textContent).toMatch(/^Not connected yet: The passkey prompt closed/)
    expect(submit).not.toHaveBeenCalled()
    expect(m.reportConnect).toHaveBeenCalledWith(expect.objectContaining({ step: 'preflight', outcome: 'cancelled' }))
    expect(screen.getByRole('button', { name: 'Connect Salesforce' })).toBeTruthy()
    submit.mockRestore()
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
    expect(await screen.findByText('Disconnected. A machine will revoke it shortly.')).toBeTruthy()
  })

  it('shows Reconnect, and a not-configured provider as Not set up', async () => {
    m.listAgentConnections.mockResolvedValue({
      providers: [PROVIDER, { name: 'other', title: 'Other', configured: false }],
      connections: [conn({ needs_reconnect: true })],
    })
    show()
    expect((await screen.findByRole('button', { name: 'Reconnect' })).closest('form')).toBeTruthy()
    // No disabled button: a provider nobody set up is a pill, and a link for those who may.
    expect(screen.queryByRole('button', { name: 'Connect Other' })).toBeNull()
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

  it('a reconnect that did not finish needs a reconnect, with no other step to take', async () => {
    m.listAgentConnections.mockResolvedValue({ providers: [PROVIDER], connections: [conn({ state: 'unshared' })] })
    show()
    expect(await screen.findByText(/The last reconnect didn't finish, so eva can't use it\./)).toBeTruthy()
    expect(screen.getByText('Reconnect needed')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Reconnect' })).toBeTruthy()
    expect(screen.queryByRole('link', { name: /Share|access|Store/ })).toBeNull()
  })

  it('never lists an agent as lent its own account', async () => {
    m.listAgentConnections.mockResolvedValue({
      providers: [PROVIDER],
      connections: [conn({ statement: { version: 2, agents: ['eva'], my_computers: [], endorsers: [] }, holders: ['cloud-ec2-live'] })],
    })
    show()
    expect(await screen.findByText('Held by: cloud-ec2-live')).toBeTruthy()
    expect(screen.queryByText(/Lent to/)).toBeNull()
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

  it('hides the share links from an admin who did not connect it, and says who can change it', async () => {
    me.current = 'other@dimagi.com'
    m.listAgentConnections.mockResolvedValue({
      providers: [PROVIDER],
      connections: [conn({ state: 'unshared' }), conn({ id: 4, shared_with: ['echo'], waiting: [{ agent: 'echo', runner: 'b' }],
        statement: { version: 2, agents: ['echo'], my_computers: [], endorsers: [] } })],
    })
    show()
    expect((await screen.findAllByText('Connected by jj@dimagi.com; only they can change who uses it.'))).toHaveLength(2)
    expect(screen.queryByRole('link', { name: /Share|Change who|Give waiting|check its machines/ })).toBeNull()
    expect(screen.getByText('Lent to: echo')).toBeTruthy()
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

  it('says so when a connection lent to it is not shared yet', async () => {
    m.listAgentConnections.mockResolvedValue({ providers: [PROVIDER], connections: [conn({ granted_from: 'jj@dimagi.com', state: 'unshared', usable: true })] })
    show()
    expect(await screen.findByText('Not shared yet: its owner must share it again before this agent can use it.')).toBeTruthy()
  })

  it('tells someone else who may change an unshared connection, without nesting paragraphs', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    me.current = 'ada@dimagi.com'
    m.listAgentConnections.mockResolvedValue({ providers: [PROVIDER], connections: [conn({ state: 'unshared' })] })
    show()
    const line = await screen.findByText(/Connected by jj@dimagi.com; only they can change who uses it\./)
    expect(line.closest('p')?.parentElement?.closest('p')).toBeNull()
    expect(error.mock.calls.join(' ')).not.toContain('validateDOMNesting')
    error.mockRestore()
  })

  it('does not say connected by nobody', async () => {
    me.current = 'ada@dimagi.com'
    m.listAgentConnections.mockResolvedValue({ providers: [PROVIDER], connections: [conn({ connected_by_email: '' })] })
    show()
    expect(await screen.findByText(/The person who connected it no longer has an account\./)).toBeTruthy()
    expect(screen.queryByText(/Connected by ;/)).toBeNull()
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
        <AgentConnectionsPanel />
      </MemoryRouter>,
    )
    expect(await screen.findByText('Connecting the service failed. Try again.')).toBeTruthy()
    expect(screen.queryByText(/evil|money/)).toBeNull()
  })

  it('names the wrong_account refusal (an admin signed in as themselves for the agent)', async () => {
    m.listAgentConnections.mockResolvedValue({ providers: [PROVIDER], connections: [] })
    render(
      <MemoryRouter initialEntries={['/x?connection=salesforce&status=error&reason=wrong_account']}>
        <AgentConnectionsPanel />
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

  describe('screens that may use the account', () => {
    const calls = (over: Record<string, unknown> = {}) => conn({ provider_calls: true, server_use: true, ...over })
    const line = (method: string, path: string) => ({ method, path })
    const share = (over: Record<string, unknown> = {}) => ({
      id: 5, kind: 'project', ref: '7', title: 'Lead intake', account_label: 'eva@dimagi-ai.com',
      approved_reads: [line('GET', 'sobjects/Lead/*')], approved_writes: [],
      pending_reads: [], pending_writes: [], needs_approval: false, ...over,
    })
    const open = async (c = calls()) => {
      m.listAgentConnections.mockResolvedValue({ providers: [PROVIDER], connections: [c] })
      m.role.current = 'admin'
      show()
      return screen.findByText('Project screens that may use eva’s account')
    }
    const named = { slug: 'eva', name: 'Eva' }
    void named

    it('lists the shared screens with what they may read and change, and the one sentence', async () => {
      m.listSurfaceShares.mockResolvedValue([share({ approved_writes: [line('POST', 'actions/custom/flow/Create_Lead_From_Contact')] })])
      await open()
      expect(await screen.findByText('Lead intake')).toBeTruthy()
      expect(screen.getByText('Read Leads')).toBeTruthy()
      expect(screen.getByText('Run the flow Create_Lead_From_Contact')).toBeTruthy()
      expect(screen.getByText('Anyone who can open that screen acts as eva in Salesforce.')).toBeTruthy()
      expect(screen.queryByRole('button', { name: 'Approve changes' })).toBeNull()
    })

    it('adds a project: preview in plain words, then Approve sends exactly the lines seen', async () => {
      m.previewSurfaceShare.mockResolvedValue({
        kind: 'project', ref: '7', title: 'Lead intake', reads: [line('GET', 'sobjects/Lead/*')], writes: [line('PATCH', 'sobjects/Lead/*')],
      })
      m.approveSurfaceShare.mockResolvedValue(share())
      await open()
      fireEvent.change(await screen.findByLabelText('Screen to add'), { target: { value: 'project:7' } })
      fireEvent.click(screen.getByRole('button', { name: 'Look at what it asks for' }))
      expect(await screen.findByText('Read Leads')).toBeTruthy()
      expect(screen.getByText('Change Leads')).toBeTruthy()
      fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
      await waitFor(() => expect(m.approveSurfaceShare).toHaveBeenCalledWith(3, 'project', '7', {
        reads: [line('GET', 'sobjects/Lead/*')], writes: [line('PATCH', 'sobjects/Lead/*')],
      }))
      expect(m.previewSurfaceShare).toHaveBeenCalledWith(3, 'project', '7')
    })

    it('adds an installed app', async () => {
      m.previewSurfaceShare.mockResolvedValue({ kind: 'app', ref: 'w/crm', title: 'CRM', reads: [line('GET', 'query')], writes: [] })
      m.approveSurfaceShare.mockResolvedValue(share({ kind: 'app', ref: 'w/crm', title: 'CRM' }))
      await open()
      fireEvent.change(await screen.findByLabelText('Screen to add'), { target: { value: 'app:w/crm' } })
      fireEvent.click(screen.getByRole('button', { name: 'Look at what it asks for' }))
      fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
      await waitFor(() => expect(m.approveSurfaceShare).toHaveBeenCalledWith(3, 'app', 'w/crm', expect.anything()))
    })

    it('a changed declaration reloads the preview and says so', async () => {
      const { ApiError } = await import('@/api/problem')
      m.previewSurfaceShare
        .mockResolvedValueOnce({ kind: 'project', ref: '7', title: 'Lead intake', reads: [line('GET', 'sobjects/Lead/*')], writes: [] })
        .mockResolvedValueOnce({ kind: 'project', ref: '7', title: 'Lead intake', reads: [line('GET', 'sobjects/Lead/*')], writes: [line('DELETE', 'sobjects/Lead/*')] })
      m.approveSurfaceShare.mockRejectedValue(new ApiError(409, 'changed', { error: 'declaration_changed', detail: 'changed' }))
      await open()
      fireEvent.change(await screen.findByLabelText('Screen to add'), { target: { value: 'project:7' } })
      fireEvent.click(screen.getByRole('button', { name: 'Look at what it asks for' }))
      fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
      expect(await screen.findByText('This screen changed while you were looking. Check what it asks for now.')).toBeTruthy()
      expect(await screen.findByText('Delete Leads')).toBeTruthy()
    })

    it('needs_approval shows Approve changes with what is new, and approving goes through the preview', async () => {
      m.listSurfaceShares.mockResolvedValue([share({ needs_approval: true, pending_writes: [line('PATCH', 'sobjects/Lead/*')] })])
      m.previewSurfaceShare.mockResolvedValue({
        kind: 'project', ref: '7', title: 'Lead intake', reads: [line('GET', 'sobjects/Lead/*')], writes: [line('PATCH', 'sobjects/Lead/*')],
      })
      m.approveSurfaceShare.mockResolvedValue(share())
      await open()
      fireEvent.click(await screen.findByRole('button', { name: 'Approve changes' }))
      expect(await screen.findByText('Change Leads')).toBeTruthy()
      expect(m.approveSurfaceShare).not.toHaveBeenCalled()
      fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
      await waitFor(() => expect(m.approveSurfaceShare).toHaveBeenCalled())
    })

    it('Stop ends a share', async () => {
      m.listSurfaceShares.mockResolvedValue([share()])
      await open()
      fireEvent.click(await screen.findByRole('button', { name: 'Stop' }))
      await waitFor(() => expect(m.stopSurfaceShare).toHaveBeenCalledWith(3, 5))
    })

    it('with the switch off there is no add control, only the reason', async () => {
      await open(calls({ server_use: false }))
      expect(screen.getByText('Turn on Used by canopy-web first.')).toBeTruthy()
      expect(screen.queryByLabelText('Screen to add')).toBeNull()
    })

    it.each(['editor', 'member', null])('whoever the panel loads for gets the controls, whatever their workspace role (%s)', async (role) => {
      m.listSurfaceShares.mockResolvedValue([share({ needs_approval: true, pending_reads: [line('GET', 'query')] })])
      m.listAgentConnections.mockResolvedValue({ providers: [PROVIDER], connections: [calls()] })
      m.role.current = role
      show()
      await screen.findByText('Project screens that may use eva’s account')
      expect(await screen.findByLabelText('Screen to add')).toBeTruthy()
      expect(await screen.findByRole('button', { name: 'Stop' })).toBeTruthy()
      expect(screen.getByRole('button', { name: 'Approve changes' })).toBeTruthy()
    })

    it('a 404 on the shares shows no share section, and no error', async () => {
      const { ApiError } = await import('@/api/problem')
      m.listSurfaceShares.mockRejectedValue(new ApiError(404, 'Not Found', { title: 'Not Found' }))
      m.listAgentConnections.mockResolvedValue({ providers: [PROVIDER], connections: [calls()] })
      show()
      await waitFor(() => expect(m.listSurfaceShares).toHaveBeenCalled())
      await screen.findByTestId('agent-connection-3')
      await waitFor(() => expect(screen.queryByTestId('screen-shares-3')).toBeNull())
      expect(screen.queryByText(/Could not read/)).toBeNull()
      expect(screen.queryByText('Project screens that may use eva’s account')).toBeNull()
    })

    it('another failure to read the shares says so', async () => {
      m.listSurfaceShares.mockRejectedValue(new Error('boom'))
      m.listAgentConnections.mockResolvedValue({ providers: [PROVIDER], connections: [calls()] })
      show()
      expect(await screen.findByTestId('screen-shares-3')).toBeTruthy()
    })

    it("the picker lists the workspace's projects, not the agent's", async () => {
      m.listAgentProjects.mockResolvedValue([{ id: 99, handle: 'mine', name: 'Only Eva', workspace: 'w' }])
      m.listWorkspaceProjects.mockResolvedValue([
        { id: 7, handle: 'leads', name: 'Lead intake', workspace: 'w' },
        { id: 8, handle: 'kpi', name: 'KPI board', workspace: 'w' },
        { id: 9, handle: 'away', name: 'Elsewhere', workspace: 'other' },
      ])
      await open()
      expect(await screen.findByRole('option', { name: 'Project: KPI board' })).toBeTruthy()
      expect(m.listWorkspaceProjects).toHaveBeenCalledWith('open', 'w')
      expect(m.listAgentProjects).not.toHaveBeenCalled()
      expect(screen.queryByRole('option', { name: 'Project: Elsewhere' })).toBeNull()
    })

    it('says writes before reads, with an agent search in the agent\'s name', async () => {
      m.listSurfaceShares.mockResolvedValue([share({
        approved_reads: [line('GET', 'query')], approved_writes: [line('DELETE', 'sobjects/Lead/*'), line('POST', 'apexrest/Thing')],
      })])
      await open()
      const items = (await screen.findAllByRole('listitem')).filter((li) => !li.hasAttribute('data-testid')).map((li) => li.textContent)
      expect(items).toEqual(['Delete Leads', 'Run the Salesforce action Thing', 'Search any record eva can see'])
    })

    it('a workspace admin who is not the agent\'s admin sees no read failure', async () => {
      const { ApiError } = await import('@/api/problem')
      m.listAgentConnections.mockRejectedValue(new ApiError(403, 'admin only', { title: 'Forbidden' }))
      m.role.current = 'admin'
      show()
      await waitFor(() => expect(m.listAgentConnections).toHaveBeenCalled())
      await waitFor(() => expect(screen.queryByText('Could not read the agent’s connections.')).toBeNull())
    })
  })
})
