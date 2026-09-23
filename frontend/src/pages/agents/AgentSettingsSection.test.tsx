// @vitest-environment jsdom
//
// Every control that configures an agent, on one page. These cases came from
// AgentOverviewSection.test.tsx: the controls were sections of a dashboard, so
// "change something about this agent" had no destination and the page had no
// name that said configuration.
import { cleanup, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

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
vi.mock('@/components/agents/AgentAdminsControl', () => ({ AgentAdminsControl: () => <div>admins-control</div> }))
vi.mock('@/components/agents/AgentInterfaceView', () => ({ AgentInterfaceView: () => <div>interface-view</div> }))
vi.mock('@/pages/agents/AgentVaultSection', () => ({ AgentVaultSection: () => null }))
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useOutletContext: () => ({
    agent: {
      slug: 'hal', name: 'Hal', persona: 'The fleet engineer.', description: '', workspace: 'connect',
      task_count: 1, sync_count: 2, work_product_count: 3, skill_count: 4,
      turn_mode: 'manual', slack_enabled: false, owner: null, can_transfer_owner: false,
    },
  }),
}))

const { AgentSettingsSection } = await import('./AgentSettingsSection')
const { CredentialsRedirect } = await import('./CredentialsRedirect')

afterEach(() => cleanup())

function Where() {
  const l = useLocation()
  return <div data-testid="where">{l.pathname + l.search + l.hash}</div>
}

describe('AgentSettingsSection', () => {
  it('holds every control, grouped by the question it answers', async () => {
    render(
      <MemoryRouter initialEntries={['/w/connect/agents/hal/settings']}>
        <Routes><Route path="/w/:workspace/agents/:slug/settings" element={<AgentSettingsSection />} /></Routes>
      </MemoryRouter>,
    )
    for (const title of ['Who operates it', 'Who can reach it', 'How it runs', 'Credentials']) {
      expect(screen.getByRole('heading', { level: 2, name: title })).toBeTruthy()
      expect(screen.getByRole('link', { name: title }).getAttribute('href')).toMatch(/^#/)
    }
    // Each control is present, under the question it belongs to.
    const where = (region: string, name: string) =>
      within(screen.getByRole('region', { name: region })).getByRole('heading', { level: 3, name })
    expect(where('Who operates it', 'Owner')).toBeTruthy()
    expect(where('Who operates it', 'Admins')).toBeTruthy()
    expect(where('Who can reach it', 'Callers')).toBeTruthy()
    expect(where('Who can reach it', 'Slack')).toBeTruthy()
    // Turn mode and runners are one table now: a rule can set both.
    expect(where('How it runs', 'Routing')).toBeTruthy()

    // Who may change each one is on the page, not discovered by an error.
    expect(within(screen.getByRole('region', { name: 'Who can reach it' }))
      .getByText('Workspace owners')).toBeTruthy()

    const credentials = screen.getByRole('region', { name: 'Credentials' })
    expect(await within(credentials).findByTestId('cred-CANOPY_PAT')).toBeTruthy()
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
    expect(screen.getByTestId('where').textContent)
      .toBe('/w/connect/agents/hal/settings?google=ok#credentials')
  })
})
