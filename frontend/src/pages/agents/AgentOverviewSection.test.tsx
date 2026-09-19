// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  listAgentSyncs: vi.fn(async () => ({ items: [], count: 0 })),
  listAgentTasks: vi.fn(async () => []),
  getAgentCredentialStatus: vi.fn(async () => [
    { name: 'CANOPY_PAT', set: true, declared: true, source: 'canopy', updated_at: null, updated_by_email: '' },
  ]),
}))
// The settings controls each fetch their own state; what is under test here is
// where they sit on the page, not their behaviour (they have their own tests).
vi.mock('@/components/agents/RunnerAssignments', () => ({ RunnerAssignments: () => <div>runners-control</div> }))
vi.mock('@/components/agents/AgentOwnerControl', () => ({ AgentOwnerControl: () => <div>owner-control</div> }))
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

const { AgentOverviewSection } = await import('./AgentOverviewSection')
const { CredentialsRedirect } = await import('./CredentialsRedirect')

afterEach(() => cleanup())

function Where() {
  const l = useLocation()
  return <div data-testid="where">{l.pathname + l.search + l.hash}</div>
}

describe('AgentOverviewSection', () => {
  it('holds every setting and the credentials, each in a titled section', async () => {
    render(
      <MemoryRouter initialEntries={['/w/connect/agents/hal/overview']}>
        <Routes><Route path="/w/:workspace/agents/:slug/overview" element={<AgentOverviewSection />} /></Routes>
      </MemoryRouter>,
    )
    for (const title of ['About', 'Take a turn', 'Activity', 'Settings', 'Credentials']) {
      expect(screen.getByRole('heading', { level: 2, name: title })).toBeTruthy()
      expect(screen.getByRole('link', { name: title }).getAttribute('href')).toMatch(/^#/)
    }
    const settings = screen.getByRole('region', { name: 'Settings' })
    for (const name of ['Owner', 'Turn mode', 'Slack', 'Runners']) {
      expect(within(settings).getByRole('heading', { level: 3, name })).toBeTruthy()
    }
    // Who may change each one is on the page, not discovered by an error.
    expect(within(settings).getByText('Workspace owners')).toBeTruthy()

    const credentials = screen.getByRole('region', { name: 'Credentials' })
    expect(await within(credentials).findByTestId('cred-CANOPY_PAT')).toBeTruthy()
  })

  it('sends the old Credentials address to its section, keeping the query', () => {
    render(
      <MemoryRouter initialEntries={['/w/connect/agents/hal/credentials?google=ok']}>
        <Routes>
          <Route path="/w/:workspace/agents/:slug">
            <Route path="credentials" element={<CredentialsRedirect />} />
            <Route path="overview" element={<Where />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.getByTestId('where').textContent).toBe('/w/connect/agents/hal/overview?google=ok#credentials')
  })
})
