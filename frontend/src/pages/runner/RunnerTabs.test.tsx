// @vitest-environment jsdom
//
// The runner page's tabs (RunnerOverview.tsx, RunnerTabs.tsx) — what the supervisor's
// one-column RunnerDetail used to hold, and the same rules about who sees what.
//
// The fleet list is workspace-scoped (apps/harness/api.py::_runner_read_q), so
// this view can now open a runner the caller did NOT pair — a teammate's laptop,
// or the box an agent identity routes through. Acting on one is still owner-only,
// and `can_manage` is how the row says which it is. Offering owner-only controls
// on a runner you don't own would recreate, in the UI, exactly the "listed a
// runner every action then 404s on" failure the split predicate had to give up.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { RunnerOut } from '@/api/harness'
import type { AgentOut } from '@/api/agents'

const pauseRunner = vi.fn<(id: string, note?: string) => Promise<RunnerOut>>()
const unpauseRunner = vi.fn<(id: string) => Promise<RunnerOut>>()
const setRunnerSessions = vi.fn<(r: RunnerOut, on: boolean) => Promise<RunnerOut>>()
const retireRunner = vi.fn<(id: string) => Promise<void>>()

vi.mock('@/api/harness', () => ({
  pauseRunner,
  unpauseRunner,
  setRunnerSessions,
  retireRunner,
  // The administrators panel reads on mount; who may administer a box is its own
  // component's subject (RunnerAdmins.test.tsx), so here it just has to resolve.
  listRunnerAdmins: vi.fn().mockResolvedValue([]),
  grantRunnerAdmin: vi.fn(),
  revokeRunnerAdmin: vi.fn(),
  listRunnerWorkspaces: vi.fn().mockResolvedValue([]),
  setRunnerOnlyAgents: vi.fn(),
  setRunnerDirected: vi.fn(),
  setRunnerPrivate: vi.fn(),
  enrollRunnerWorkspace: vi.fn(),
  unenrollRunnerWorkspace: vi.fn(),
  // The Repos section (RunnerProjects) reads on mount.
  listRunnerProjects: vi.fn().mockResolvedValue({ projects: ['ada'], requests: [], can_add: true }),
  addRunnerProject: vi.fn(),
  deleteRunnerProjectRequest: vi.fn(),
}))
vi.mock('@/api/workspaces', () => ({
  listMembers: vi.fn().mockResolvedValue([]),
}))
vi.mock('@/api/drills', () => ({
  startDrill: vi.fn(),
  listDrills: vi.fn().mockResolvedValue([]),
}))
vi.mock('@/components/agents/RunnerAssignments', () => ({
  // Agent routing is gated on the AGENT's workspace, not on runner ownership,
  // so it stays available to any member and is stubbed rather than exercised.
  RunnerAssignments: () => <div data-testid="assignments-stub" />,
}))

// What the alert list shows is AlertList.test.tsx's subject; here only which
// scope this view asks it for.
vi.mock('@/components/alerts/AlertList', () => ({
  AlertList: ({ scope, hideEmpty }: { scope?: string; hideEmpty?: boolean }) => (
    <div data-testid="alert-list-stub" data-scope={scope} data-hide-empty={String(!!hideEmpty)} />
  ),
}))

// The credentials list is RunnerCredentials.test.tsx's subject; here only who sees it.
vi.mock('@/components/supervisor/RunnerCredentials', () => ({
  RunnerCredentials: () => <div data-testid="runner-credentials" />,
  boxLoginAccount: () => '',
}))

const { RunnerOverview } = await import('./RunnerOverview')
const { RunnerAccessTab, RunnerCredentialsTab, RunnerRoutingTab, RunnerSettingsTab } = await import('./RunnerTabs')
const { runnerTabs } = await import('./RunnerPage')

function runner(overrides: Partial<RunnerOut> = {}): RunnerOut {
  return {
    id: 'r1',
    name: 'jj-mbp-cdp',
    kind: 'emdash',
    status: 'online',
    status_note: '',
    ready: true,
    ready_note: '',
    paused: false,
    paused_note: '',
    paused_at: null,
    last_heartbeat_at: '2026-07-28T20:00:00Z',
    capabilities: { projects: ['canopy-web'] },
    host: '',
    code_branch: '',
    code_version: '',
    code_sha: '',
    expected_code_sha: '',
    workspace: 'dimagi',
    owner_email: 'jjackson@dimagi.com',
    can_manage: true,
    can_administer: true,
    flags: [],
    known_flags: ['sensitive_data'],
    ...overrides,
  } as RunnerOut
}

const agents: AgentOut[] = []

function ctx(r: RunnerOut, over: { onChanged?: (r: RunnerOut) => void; onRetired?: () => void } = {}) {
  return { runner: r, agents, onChanged: over.onChanged ?? (() => {}), onRetired: over.onRetired ?? (() => {}) }
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('runner page — which tabs a viewer gets', () => {
  it('offers Credentials only on a cloud box the viewer administers', () => {
    const labels = (r: RunnerOut) => runnerTabs(r).map((t) => t.label)
    expect(labels(runner({ kind: 'cloud' }))).toEqual(['Overview', 'Credentials', 'Routing', 'Access', 'Settings'])
    // Membership is not administration: this workspace auto-joins a whole email
    // domain, so following membership would hand out the fleet's credentials.
    expect(labels(runner({ kind: 'cloud', can_administer: false, can_manage: false }))).not.toContain('Credentials')
    expect(labels(runner({ kind: 'emdash' }))).not.toContain('Credentials')
  })

  it('marks Credentials when one needs signing in again', () => {
    const t = runnerTabs(runner({ kind: 'cloud' }), true).find((x) => x.label === 'Credentials')
    expect(t?.dot).toBe(true)
  })
})

describe('Overview', () => {
  it('shows the alerts scoped to this runner, and nothing when there are none', () => {
    render(<RunnerOverview {...ctx(runner())} />)
    const stub = screen.getByTestId('alert-list-stub')
    expect(stub.getAttribute('data-scope')).toBe('runner:r1')
    expect(stub.getAttribute('data-hide-empty')).toBe('true')
  })

  it('offers the drill control on a runner the caller paired', () => {
    render(<RunnerOverview {...ctx(runner())} />)
    expect(screen.getByTestId('runner-drills')).toBeTruthy()
  })

  it('hides the readiness check from a member who neither paired nor administers it', () => {
    render(<RunnerOverview {...ctx(runner({ can_manage: false, can_administer: false }))} />)
    expect(screen.queryByTestId('runner-drills')).toBeNull()
  })

  it('offers the readiness check to a runner admin who did not pair it', () => {
    render(<RunnerOverview {...ctx(runner({ can_manage: false, can_administer: true }))} />)
    expect(screen.getByTestId('runner-drills')).toBeTruthy()
  })

  it('says whose runner it is instead of just removing the controls', () => {
    render(<RunnerOverview {...ctx(runner({ can_manage: false }))} />)
    expect(screen.getByTestId('runner-detail-readonly').textContent).toContain('jjackson@dimagi.com')
  })

  it('tells an admin who is not the owner that they may declare the box a sensitive-data runner', () => {
    render(<RunnerOverview {...ctx(runner({ can_manage: false, can_administer: true }))} />)
    expect(screen.getByTestId('runner-detail-readonly').textContent).toMatch(/declare what it runs with/)
  })

  it('tells someone read-only that a runner admin can change what it declares', () => {
    render(<RunnerOverview {...ctx(runner({ can_manage: false, can_administer: false }))} />)
    expect(screen.getByTestId('runner-detail-readonly').textContent).toMatch(/or a runner admin\s+can change what it declares/)
  })
})

describe('Credentials', () => {
  it('shows the credentials to an administrator who did not pair it', () => {
    // The defect this guards: both were gated on can_manage, so the identity a
    // cloud box RUNS AS could not sign it back in.
    render(<RunnerCredentialsTab {...ctx(runner({ can_manage: false, can_administer: true, kind: 'cloud' }))} />)
    expect(screen.getByTestId('runner-credentials')).toBeTruthy()
  })

  it('withholds credentials from a member with no grant', () => {
    render(<RunnerCredentialsTab {...ctx(runner({ can_manage: false, can_administer: false, kind: 'cloud' }))} />)
    expect(screen.queryByTestId('runner-credentials')).toBeNull()
  })
})

// The remote half of ~/.canopy/PAUSED — and the ONLY way to park a box from a
// phone, where there is no shell on that macOS account to drop a sentinel in.
describe('Settings — pause', () => {
  it('pauses with the typed reason and re-renders from the server’s row', async () => {
    const paused = runner({ paused: true, paused_note: 'token limit', status: 'paused' })
    pauseRunner.mockResolvedValue(paused)
    const onChanged = vi.fn()
    render(<RunnerSettingsTab {...ctx(runner(), { onChanged })} />)
    fireEvent.click(screen.getByTestId('runner-pause-toggle'))
    fireEvent.change(screen.getByTestId('runner-pause-note'), { target: { value: 'token limit' } })
    await act(async () => {
      fireEvent.click(screen.getByTestId('runner-pause-confirm'))
    })
    expect(pauseRunner).toHaveBeenCalledWith('r1', 'token limit')
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(paused))
  })

  it('says when a usage-cap pause lifts on its own', () => {
    render(<RunnerSettingsTab {...ctx(runner({
      paused: true, status: 'paused',
      paused_note: 'Claude usage cap — resumes 2026-10-08T08:30Z',
      unpause_at: '2026-10-08T08:30:00Z',
    }))} />)
    expect(screen.getByTestId('runner-unpause-at').textContent).toContain('Resumes on its own')
  })

  it('offers Resume and the recorded reason once paused', async () => {
    unpauseRunner.mockResolvedValue(runner())
    render(<RunnerSettingsTab {...ctx(runner({ paused: true, paused_note: 'token limit on this account', status: 'paused' }))} />)
    expect(screen.getByTestId('runner-pause-why').textContent).toContain('token limit on this account')
    expect(screen.queryByTestId('runner-pause-note')).toBeNull()
    await act(async () => {
      fireEvent.click(screen.getByTestId('runner-pause-toggle'))
    })
    expect(unpauseRunner).toHaveBeenCalledWith('r1')
  })

  it('says so when the call fails rather than looking paused', async () => {
    pauseRunner.mockRejectedValue(new Error('pauseRunner failed: 404'))
    render(<RunnerSettingsTab {...ctx(runner())} />)
    fireEvent.click(screen.getByTestId('runner-pause-toggle'))
    await act(async () => {
      fireEvent.click(screen.getByTestId('runner-pause-confirm'))
    })
    expect(screen.getByTestId('runner-pause-error').textContent).toContain('404')
  })

  it('is absent on a runner the caller did not pair — POST /pause would 404', () => {
    render(<RunnerSettingsTab {...ctx(runner({ can_manage: false }))} />)
    expect(screen.queryByTestId('runner-pause')).toBeNull()
  })
})

describe('Settings — Slack & chat sessions', () => {
  it('lets the owner turn sessions on and re-renders from the server row', async () => {
    const on = runner({ capabilities: { agents: ['hal'], sessions: true } })
    setRunnerSessions.mockResolvedValue(on)
    const onChanged = vi.fn()
    const off = runner({ capabilities: { agents: ['hal'] } })
    render(<RunnerSettingsTab {...ctx(off, { onChanged })} />)
    expect(screen.getByTestId('runner-sessions').textContent).toContain('hidden from')
    await act(async () => {
      fireEvent.click(screen.getByTestId('runner-sessions-toggle'))
    })
    expect(setRunnerSessions).toHaveBeenCalledWith(off, true)
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(on))
  })

  it('shows the setting read-only to someone who did not pair the box', () => {
    render(<RunnerSettingsTab {...ctx(runner({ can_manage: false, can_administer: false, capabilities: { sessions: true } }))} />)
    expect(screen.queryByTestId('runner-sessions-toggle')).toBeNull()
    expect(screen.getByTestId('runner-sessions-state').textContent).toBe('on')
  })

  it('says when a box is directed only', () => {
    render(<RunnerSettingsTab {...ctx(runner({ directed: true, capabilities: { sessions: true } }))} />)
    expect(screen.getByTestId('runner-directed').textContent).toContain('Directed only')
  })

  it('says nothing about it on an ordinary box', () => {
    render(<RunnerSettingsTab {...ctx(runner({ capabilities: { sessions: true } }))} />)
    expect(screen.queryByTestId('runner-directed')).toBeNull()
  })
})

describe('Settings — retire', () => {
  it('retires only after a second, explicit click', async () => {
    retireRunner.mockResolvedValue(undefined)
    const onRetired = vi.fn()
    render(<RunnerSettingsTab {...ctx(runner(), { onRetired })} />)
    fireEvent.click(screen.getByTestId('runner-retire-toggle'))
    expect(retireRunner).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('runner-retire-confirm'))
    await waitFor(() => expect(onRetired).toHaveBeenCalled())
    expect(retireRunner).toHaveBeenCalledWith('r1')
  })

  it('offers no Retire on a box the caller may not manage', () => {
    render(<RunnerSettingsTab {...ctx(runner({ can_manage: false }))} />)
    expect(screen.queryByTestId('runner-retire')).toBeNull()
  })
})

describe('Routing and Access', () => {
  it('lists every agent, each opening its own routing', () => {
    const hal = { slug: 'hal', name: 'Hal', workspace: 'dimagi', turn_mode: 'manual' } as AgentOut
    render(<RunnerRoutingTab {...ctx(runner())} agents={[hal]} />)
    expect(screen.getByTestId('runner-priority-agent-hal')).toBeTruthy()
  })

  it('names the owner to someone who cannot change who administers it', () => {
    render(<RunnerAccessTab {...ctx(runner({ can_manage: false, can_administer: false }))} />)
    expect(screen.getByTestId('runner-access-readonly').textContent).toContain('jjackson@dimagi.com')
  })
})
