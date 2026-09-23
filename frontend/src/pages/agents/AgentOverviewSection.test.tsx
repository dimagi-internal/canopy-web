// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
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

const { AgentOverviewSection } = await import('./AgentOverviewSection')

afterEach(() => cleanup())

describe('AgentOverviewSection', () => {
  it('is a dashboard again: no settings, no credentials, and it says where they went', () => {
    render(
      <MemoryRouter initialEntries={['/w/connect/agents/hal/overview']}>
        <Routes><Route path="/w/:workspace/agents/:slug/overview" element={<AgentOverviewSection />} /></Routes>
      </MemoryRouter>,
    )
    for (const title of ['About', 'Take a turn', 'Activity']) {
      expect(screen.getByRole('heading', { level: 2, name: title })).toBeTruthy()
    }
    expect(screen.queryByRole('region', { name: 'Settings' })).toBeNull()
    expect(screen.queryByRole('region', { name: 'Credentials' })).toBeNull()
    // A page that silently loses what you came for is worse than the long page
    // it replaced, so it points at where the controls live now.
    const moved = screen.getByTestId('settings-moved')
    expect(moved.textContent).toContain('credentials')
    expect(within(moved).getByRole('link', { name: 'Settings' }).getAttribute('href'))
      .toContain('settings')
  })
})
