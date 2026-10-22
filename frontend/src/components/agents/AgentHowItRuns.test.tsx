// @vitest-environment jsdom
//
// The two ways an agent runs, as its Settings shows them: in your own Claude Code
// (no runner) and on its own (a runner). The lights are pure functions of the
// reads, tested alone; the render checks what each card lists.
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AgentLicenseOut } from '@/api/agents'
import type { RunnerDeviceKeyOut } from '@/api/deviceKeys'
import type { RunnerOut } from '@/api/harness'
import type { ZkStateOut } from '@/api/vault'

const api = vi.hoisted(() => ({
  listAgentLicenses: vi.fn(),
  getAgentDefaultOrder: vi.fn(),
  getAgentRunnerRules: vi.fn(),
  listRunners: vi.fn(),
  listRunnerDeviceKeys: vi.fn(),
  getAgentDeviceKeys: vi.fn(),
  listSchedules: vi.fn(),
  getZkState: vi.fn(),
}))
vi.mock('@/api/agents', () => ({
  listAgentLicenses: api.listAgentLicenses,
  getAgentDefaultOrder: api.getAgentDefaultOrder,
  getAgentRunnerRules: api.getAgentRunnerRules,
}))
vi.mock('@/api/harness', () => ({ listRunners: api.listRunners }))
vi.mock('@/api/deviceKeys', () => ({
  listRunnerDeviceKeys: api.listRunnerDeviceKeys,
  getAgentDeviceKeys: api.getAgentDeviceKeys,
}))
vi.mock('@/api/schedules', () => ({ listSchedules: api.listSchedules }))
vi.mock('@/api/vault', () => ({ getZkState: api.getZkState }))
vi.mock('@/auth/AuthProvider', () => ({
  useAuth: () => ({ status: 'authenticated', user: { email: 'me@dimagi.com' } }),
}))

const { AgentHowItRuns, viewerReadiness, vaultReach } = await import('./AgentHowItRuns')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const runner = (id: string, over: Partial<RunnerOut> = {}) =>
  ({ id, name: id, status: 'online', ready: true, private: false, owner_email: 'x@dimagi.com', ...over }) as unknown as RunnerOut
const key = (id: string, runnerId: string, over: Partial<RunnerDeviceKeyOut> = {}) =>
  ({ id, runner: runnerId, runner_name: runnerId, status: 'active', can_open: [], zk_pending_agents: [], ...over }) as unknown as RunnerDeviceKeyOut
const license = (over: Partial<AgentLicenseOut> = {}) =>
  ({ user_id: 1, email: 'me@dimagi.com', name: 'Me', status: 'active', private_runners: ['my-mac'],
     last_used_at: null, ...over }) as unknown as AgentLicenseOut

describe('viewerReadiness', () => {
  const mine = runner('my-mac', { private: true, owner_email: 'me@dimagi.com' })
  const base = { slug: 'hal', email: 'me@dimagi.com', runners: [mine, runner('cloud-1')] }

  it('is off with no computer of yours', () => {
    expect(viewerReadiness({ ...base, license: undefined, keys: [key('k0', 'cloud-1')] }).light).toBe('off')
  })
  it('is ready when your computer can open its vault', () => {
    const r = viewerReadiness({ ...base, license: license(),
      keys: [key('k1', 'my-mac', { can_open: [{ slug: 'hal', name: 'Hal' }] } as never)] })
    expect(r).toEqual({ light: 'ready', text: 'Ready: my-mac can read its secrets.' })
  })
  it('waits on a key to approve, or a vault grant', () => {
    expect(viewerReadiness({ ...base, license: license(), keys: [key('k1', 'my-mac', { status: 'pending' })] }).light)
      .toBe('waiting')
    const grant = viewerReadiness({ ...base, license: license(),
      keys: [key('k1', 'my-mac', { zk_pending_agents: ['hal'] } as never)] })
    expect(grant.text).toMatch(/^Grant pending/)
  })
  it('counts a computer without the runner app, which no runner list shows', () => {
    const laptop = key('k2', 'aath-mbp', { runner_kind: 'computer', can_open: [{ slug: 'hal', name: 'Hal' }] } as never)
    expect(viewerReadiness({ ...base, license: license(), keys: [laptop] }))
      .toEqual({ light: 'ready', text: 'Ready: aath-mbp can read its secrets.' })
    expect(viewerReadiness({ ...base, license: license(),
      keys: [key('k3', 'aath-mbp', { runner_kind: 'computer', status: 'pending' } as never)] }).light).toBe('waiting')
  })
  it('says a revoked licence plainly', () => {
    expect(viewerReadiness({ ...base, license: license({ status: 'revoked' }), keys: [] }).text).toMatch(/revoked/)
  })
})

describe('vaultReach', () => {
  const zk = {
    enabled: true, kv: 2,
    wraps: [{ kind: 'device', kv: 2, target: 'k1', current: true }, { kind: 'device', kv: 1, target: 'k2', current: true }],
    pending: [{ kind: 'device', target: 'k3', runner: 'box-c' }],
  } as unknown as ZkStateOut
  const keys = [key('k1', 'box-a'), key('k2', 'box-b'), key('k3', 'box-c'), key('k4', 'box-d', { status: 'pending' })]

  it('reads, grant pending, key pending, or none', () => {
    expect(vaultReach('box-a', zk, keys)).toBe('reads')
    expect(vaultReach('box-b', zk, keys)).toBe('none') // its wrap is for an older key version
    expect(vaultReach('box-c', zk, keys)).toBe('grant-pending')
    expect(vaultReach('box-d', zk, keys)).toBe('key-pending')
  })
})

describe('AgentHowItRuns', () => {
  function setup({ admin }: { admin: boolean }) {
    api.listAgentLicenses.mockResolvedValue([license(), license({ user_id: 2, email: 'b@dimagi.com', name: 'Beth', status: 'requested' })])
    api.listRunners.mockResolvedValue([runner('cloud-1'), runner('my-mac', { private: true, owner_email: 'me@dimagi.com' })])
    api.listRunnerDeviceKeys.mockResolvedValue([])
    api.getAgentDefaultOrder.mockResolvedValue({ own: true, runners: [
      { runner_id: 'cloud-1', runner_name: 'cloud-1', kind: 'cloud', rank: 0, online: true, ready: true, enabled: true },
    ] })
    api.getAgentRunnerRules.mockResolvedValue([{ source: 'email', actor: '', rank: 0, runner_id: 'my-mac', runner_name: 'my-mac' }])
    api.listSchedules.mockResolvedValue([{ enabled: true }, { enabled: false }, { enabled: true }])
    api.getZkState.mockResolvedValue({ enabled: true, kv: 1, wraps: [], pending: [{ kind: 'device', target: 'k9', runner: 'cloud-1' }] })
    api.getAgentDeviceKeys.mockResolvedValue([key('k9', 'cloud-1')])
    render(<MemoryRouter initialEntries={['/w/connect/agents/hal/settings']}>
      <Routes>
        <Route path="/w/:workspace/agents/:slug/settings" element={<AgentHowItRuns slug="hal" name="Hal" isAdmin={admin} />} />
      </Routes>
    </MemoryRouter>)
  }

  it('shows both ways: the command and who uses it locally, and the runners it routes to', async () => {
    setup({ admin: true })
    const yourself = screen.getByRole('group', { name: 'Use it yourself in Claude Code' })
    expect(within(yourself).getByTestId('agent-use-command').textContent).toBe('canopy agent use hal')
    expect(await within(yourself).findByText('Me')).toBeTruthy()
    expect(within(yourself).queryByText('Beth')).toBeNull() // requested, not using it
    expect(within(yourself).getByText(/1 requested/)).toBeTruthy()
    expect(within(yourself).getByTestId('viewer-readiness').dataset.light).toBe('off')

    const own = screen.getByRole('group', { name: 'Let it run on its own' })
    const rows = await within(own).findByTestId('routed-runners')
    await waitFor(() => expect(within(rows).getByText(/private/)).toBeTruthy())
    expect(rows.querySelector('[data-runner="cloud-1"]')?.textContent).toMatch(/shared · ready/)
    expect(rows.querySelector('[data-runner="my-mac"]')?.textContent).toMatch(/email/)
    expect((await within(own).findByTestId('vault-cloud-1')).textContent).toBe('grant pending')
    expect(within(own).getByTestId('grant-pending').textContent).toMatch(/1 runner cannot open its vault/)
    expect(within(own).getByTestId('schedule-count').textContent).toMatch(/2 schedules on/)
    expect(within(own).getByRole('link', { name: 'Schedules' }).getAttribute('href')).toBe('/w/connect/agents/hal/schedules')
  })

  it('asks nothing of the vault for someone who is not an admin', async () => {
    setup({ admin: false })
    await screen.findByTestId('routed-runners')
    expect(api.getZkState).not.toHaveBeenCalled()
    expect(api.getAgentDeviceKeys).not.toHaveBeenCalled()
    expect(screen.queryByTestId('vault-cloud-1')).toBeNull()
  })

  it('says an agent with no runner still runs in Claude Code', async () => {
    setup({ admin: false })
    api.getAgentDefaultOrder.mockResolvedValue({ own: false, runners: [] })
    api.getAgentRunnerRules.mockResolvedValue([])
    cleanup()
    render(<MemoryRouter><AgentHowItRuns slug="hal" name="Hal" isAdmin={false} /></MemoryRouter>)
    expect((await screen.findByTestId('no-runner')).textContent).toMatch(/only when someone uses it in Claude Code/)
  })
})
