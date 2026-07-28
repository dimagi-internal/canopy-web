// @vitest-environment jsdom
//
// The fleet list is workspace-scoped (apps/harness/api.py::_runner_read_q), so
// this view can now open a runner the caller did NOT pair — a teammate's laptop,
// or the box an agent identity routes through. Acting on one is still owner-only,
// and `can_manage` is how the row says which it is. Offering owner-only controls
// on a runner you don't own would recreate, in the UI, exactly the "listed a
// runner every action then 404s on" failure the split predicate had to give up.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import type { RunnerOut } from '@/api/harness'
import type { AgentOut } from '@/api/agents'

vi.mock('@/api/drills', () => ({
  startDrill: vi.fn(),
  listDrills: vi.fn().mockResolvedValue([]),
}))
vi.mock('@/components/agents/RunnerAssignments', () => ({
  // Agent routing is gated on the AGENT's workspace, not on runner ownership,
  // so it stays available to any member and is stubbed rather than exercised.
  RunnerAssignments: () => <div data-testid="assignments-stub" />,
}))

const { RunnerDetail } = await import('./RunnerDetail')

function runner(overrides: Partial<RunnerOut> = {}): RunnerOut {
  return {
    id: 'r1',
    name: 'jj-mbp-cdp',
    kind: 'emdash',
    status: 'online',
    status_note: '',
    ready: true,
    ready_note: '',
    last_heartbeat_at: '2026-07-28T20:00:00Z',
    capabilities: { projects: ['canopy-web'] },
    host: '',
    code_branch: '',
    workspace: 'dimagi',
    paired_by_email: 'jjackson@dimagi.com',
    can_manage: true,
    drill_rollup: null,
    ...overrides,
  } as RunnerOut
}

const agents: AgentOut[] = []

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RunnerDetail', () => {
  it('offers the drill control on a runner the caller paired', () => {
    render(<RunnerDetail runner={runner()} agents={agents} onBack={() => {}} />)
    expect(screen.getByTestId('runner-drills')).toBeTruthy()
  })

  it('hides owner-only controls on a runner the caller did not pair', () => {
    render(<RunnerDetail runner={runner({ can_manage: false })} agents={agents} onBack={() => {}} />)
    // Drilling POSTs as the runner's owner; the drill LIST is owner-gated too, so
    // rendering the panel would only produce a 404 error message.
    expect(screen.queryByTestId('runner-drills')).toBeNull()
  })

  it('says whose runner it is instead of just removing the controls', () => {
    render(<RunnerDetail runner={runner({ can_manage: false })} agents={agents} onBack={() => {}} />)
    // "Nothing here" is indistinguishable from a broken page. Name the owner, so
    // "ask them to declare the repo" is an available next step.
    const note = screen.getByTestId('runner-detail-readonly')
    expect(note.textContent).toContain('jjackson@dimagi.com')
  })
})
