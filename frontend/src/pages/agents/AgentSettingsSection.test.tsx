// @vitest-environment jsdom
//
// Every control that configures an agent, behind tabs with their own
// addresses — and every old `settings#anchor` link still lands on its control.
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { Link, MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getAgentCredentialStatus: vi.fn(async () => [
    { name: 'CANOPY_PAT', set: true, declared: true, source: 'canopy', updated_at: null, updated_by_email: '' },
  ]),
}))
// The settings controls each fetch their own state; what is under test here is
// where they sit on the page, not their behaviour (they have their own tests).
vi.mock('@/components/agents/AgentRouting', () => ({ AgentRouting: () => <div>routing-control</div> }))
vi.mock('@/components/agents/AgentOwnerControl', () => ({ AgentOwnerControl: () => <div>owner-control</div> }))
vi.mock('@/components/agents/AgentAccessRoster', () => ({ AgentAccessRoster: () => <div>access-roster</div> }))
vi.mock('@/components/agents/AgentInterfaceView', () => ({ AgentInterfaceView: () => <div>interface-view</div> }))
vi.mock('@/components/agents/AgentMailboxControl', () => ({ AgentMailboxControl: () => <div>mailbox-control</div> }))
vi.mock('@/pages/agents/AgentVaultSection', () => ({ AgentVaultSection: () => null }))
vi.mock('@/pages/agents/AgentDeviceKeysSection', () => ({ AgentDeviceKeysSection: () => null }))
vi.mock('@/components/agents/AgentSetupChecklist', () => ({ AgentSetupChecklist: () => null }))
const { getAgentSetup } = vi.hoisted(() => ({ getAgentSetup: vi.fn() }))
vi.mock('@/api/agentCreation', () => ({ getAgentSetup }))
vi.mock('@/pages/agents/AgentSecurityPanel', () => ({ AgentSecurityPanel: () => <div>security-panel</div> }))
vi.mock('@/pages/agents/AgentAccountsPanel', () => ({ AgentAccountsPanel: () => <div id="cred-github">accounts-panel</div> }))
vi.mock('@/pages/agents/AgentConnectionsPanel', () => ({ AgentConnectionsPanel: () => <div>connections-panel</div> }))
vi.mock('@/components/agents/AgentHowItRuns', () => ({ AgentHowItRuns: () => <div>how-it-runs</div> }))
let agentOverride: Record<string, unknown> = {}
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useOutletContext: () => ({
    agent: {
      slug: 'hal', name: 'Hal', persona: 'The fleet engineer.', description: '', workspace: 'connect',
      task_count: 1, sync_count: 2, skill_count: 4,
      turn_mode: 'manual', slack_enabled: false, owner: null, can_transfer_owner: false,
      ...agentOverride,
    },
  }),
}))

const { AgentSettingsSection } = await import('./AgentSettingsSection')
const { CredentialsRedirect } = await import('./CredentialsRedirect')

beforeEach(() => getAgentSetup.mockResolvedValue([]))
afterEach(() => cleanup())

function Where() {
  const l = useLocation()
  return <div data-testid="where">{l.pathname + l.search + l.hash}</div>
}

const BASE = '/w/connect/agents/hal/settings'

function at(path: string, extra?: React.ReactNode) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/w/:workspace/agents/:slug/settings" element={<><Where />{extra}<AgentSettingsSection /></>} />
        <Route path="/w/:workspace/agents/:slug/settings/:tab" element={<><Where />{extra}<AgentSettingsSection /></>} />
      </Routes>
    </MemoryRouter>,
  )
}

const where = () => screen.getByTestId('where').textContent

describe('AgentSettingsSection', () => {
  it('opens on Overview, with one tab per question', async () => {
    at(BASE)
    await waitFor(() => expect(where()).toBe(`${BASE}/overview`))
    const tabs = within(screen.getByRole('navigation', { name: 'Agent settings' })).getAllByRole('link')
    expect(tabs.map((t) => t.textContent)).toEqual([
      'Overview', 'Roles', 'Channels', 'External accounts', 'Security',
    ])
    expect(tabs[4].getAttribute('href')).toBe(`${BASE}/security`)
    expect(screen.getByRole('heading', { level: 2, name: 'About' })).toBeTruthy()
    // Where it runs is part of the overview.
    expect(screen.getByRole('heading', { level: 2, name: 'Where it runs' })).toBeTruthy()
    // Only the open tab is on the page.
    expect(screen.queryByRole('heading', { level: 2, name: 'Roles' })).toBeNull()
  })

  it('holds each control on the tab for its question', async () => {
    const region = (name: string) => screen.getByRole('region', { name })
    const control = (r: string, name: string) => within(region(r)).getByRole('heading', { level: 3, name })

    at(`${BASE}/roles`)
    expect(control('Roles', 'Owner')).toBeTruthy()
    expect(control('Roles', 'People')).toBeTruthy()
    cleanup()

    at(`${BASE}/channels`)
    for (const name of ['Callers', 'Slack', 'Email', 'Google account']) expect(control('Channels', name)).toBeTruthy()
    // Who may change each one is said to someone who cannot (this viewer is
    // not an admin), not discovered by an error.
    expect(within(region('Channels')).getAllByText("Only Hal's admins can change this.")).toHaveLength(4)
    cleanup()

    at(`${BASE}/overview`)
    expect(control('Where it runs', 'Routing')).toBeTruthy()
    expect(control('Where it runs', 'Repository')).toBeTruthy()
    cleanup()

    // Security opens on its secrets, then who can open them.
    at(`${BASE}/security`)
    expect(await within(region('Secrets')).findByTestId('vault-row-CANOPY_PAT')).toBeTruthy()
    expect(region('Who can open them')).toBeTruthy()
  })

  it('forwards an old anchor to the tab that holds it, keeping query and anchor', async () => {
    at(`${BASE}#reach`)
    await waitFor(() => expect(where()).toBe(`${BASE}/channels#reach`))
    cleanup()
    at(`${BASE}#routing`)
    await waitFor(() => expect(where()).toBe(`${BASE}/overview#routing`))
    cleanup()
    at(`${BASE}#cred-github`)
    await waitFor(() => expect(where()).toBe(`${BASE}/accounts#cred-github`))
    cleanup()
    // Back from the Google sign-in, #credentials means the mailbox.
    at(`${BASE}?google=ok#credentials`)
    await waitFor(() => expect(where()).toBe(`${BASE}/accounts?google=ok#credentials`))
    cleanup()
    at(`${BASE}#credentials`)
    await waitFor(() => expect(where()).toBe(`${BASE}/security#credentials`))
  })

  it('follows an in-page link to an anchor on another tab', async () => {
    at(`${BASE}/overview`, <Link to="#credentials">creds</Link>)
    fireEvent.click(screen.getByRole('link', { name: 'creds' }))
    await waitFor(() => expect(where()).toBe(`${BASE}/security#credentials`))
  })

  it('marks the tab a setup step still needs', async () => {
    getAgentSetup.mockResolvedValue([
      { key: 'github', status: 'todo', href: `${BASE}#cred-github`, only_to_run_on_its_own: false },
      { key: 'repo', status: 'done', href: `${BASE}#repository`, only_to_run_on_its_own: false },
      { key: 'vault-key', status: 'todo', href: '/settings#vault-key', only_to_run_on_its_own: false },
    ])
    at(`${BASE}/overview`)
    const nav = screen.getByRole('navigation', { name: 'Agent settings' })
    await waitFor(() =>
      expect(within(nav).getByRole('link', { name: /External accounts/ }).querySelector('[aria-label="needs setting up"]')).toBeTruthy())
    expect(within(nav).getAllByLabelText('needs setting up')).toHaveLength(1)
  })

  it('scrolls to a section that renders late, and again when its link is clicked twice', async () => {
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    const to = `${BASE}/security#canopy-login`
    at(to, <Link to={to}>again</Link>)
    // `#canopy-login` is inside the secrets panel, which waits on its own fetch.
    const onTarget = () => scroll.mock.contexts.filter((el) => (el as Element).id === 'canopy-login').length
    await waitFor(() => expect(onTarget()).toBe(1))
    // The address already ends in #canopy-login: the checklist's link must still move the page.
    fireEvent.click(screen.getByRole('link', { name: 'again' }))
    await waitFor(() => expect(onTarget()).toBe(2))
  })

  it('sends the old Credentials address here, keeping the query', () => {
    // The Google mailbox mint returns with ?google=ok and the banner reads it,
    // so the query has to survive the hop.
    render(
      <MemoryRouter initialEntries={['/w/connect/agents/hal/credentials?google=ok']}>
        <Routes>
          <Route path="/w/:workspace/agents/:slug">
            <Route path="credentials" element={<CredentialsRedirect />} />
            <Route path="settings" element={<Where />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )
    expect(where()).toBe('/w/connect/agents/hal/settings?google=ok#credentials')
  })

  it('shows "who can change this" only to someone who cannot', () => {
    agentOverride = { is_admin: true, can_manage_admins: true, can_transfer_owner: true }
    try {
      at(`${BASE}/roles`)
      expect(screen.queryByText("Only Hal's admins can change this.")).toBeNull()
      expect(screen.queryByText(/workspace owners and Hal's owner can change this/)).toBeNull()
      // one line per setting
      expect(screen.getByText('Runs Hal. GitHub features use their GitHub connection.')).toBeTruthy()
      expect(screen.getByText('The canopy account Hal signs in as.')).toBeTruthy()
    } finally {
      agentOverride = {}
    }
  })
})
