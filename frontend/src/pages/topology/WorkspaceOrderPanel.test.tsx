// @vitest-environment jsdom
//
// A new workspace (a family's, 2026-10-18) starts with no runners. The order
// refuses a box that is not enrolled there, and enrolling lived only on each
// runner's own page — so "have my boxes run this workspace" was four detours
// and a 422. Adding your own box to the order now enrolls it on the way.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { RunnerOut } from '@/api/harness'
import type { FleetMap } from './topologyMap'

const enrollRunnerWorkspace = vi.fn<(id: string, ws: string) => Promise<unknown>>()
const getRunnerOrder = vi.fn()
const setRunnerOrder = vi.fn()

vi.mock('@/api/harness', () => ({ enrollRunnerWorkspace }))
vi.mock('@/api/workspaces', () => ({ getRunnerOrder, setRunnerOrder }))
vi.mock('@/components/agents/RepoSetupDialog', () => ({ RepoSetupDialog: () => null }))
vi.mock('@/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ workspaces: [{ slug: 'family', role: 'owner' }] }),
}))

const { WorkspaceOrderPanel } = await import('./WorkspaceOrderPanel')

const map: FleetMap = {
  root: null, agents: new Map(), runners: new Map(), outside: [], edges: new Map(),
  profiles: new Map(), orderFrom: new Map(),
}

function fleetOf(...rs: Partial<RunnerOut>[]): Map<string, RunnerOut> {
  return new Map(rs.map((r) => [r.id!, { status: 'online', kind: 'emdash', can_manage: true, ...r } as RunnerOut]))
}

function renderPanel(fleet: Map<string, RunnerOut>) {
  render(<WorkspaceOrderPanel workspace="family" map={map} fleet={fleet} onSaved={() => {}} onSelectAgent={() => {}} />)
}

async function add(id: string) {
  const select = await screen.findByTestId('workspace-order-add-select')
  fireEvent.change(select, { target: { value: id } })
  fireEvent.click(screen.getByTestId('workspace-order-add'))
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('adding a runner to a workspace order', () => {
  it('enrolls your own box in the workspace, then saves the order', async () => {
    getRunnerOrder.mockResolvedValue([])
    enrollRunnerWorkspace.mockResolvedValue({})
    setRunnerOrder.mockResolvedValue([])
    renderPanel(fleetOf({ id: 'r1', name: 'cloud-ec2-2' }))
    await add('r1')
    await waitFor(() => expect(setRunnerOrder).toHaveBeenCalledWith('family', [{ runnerId: 'r1', enabled: true }]))
    expect(enrollRunnerWorkspace).toHaveBeenCalledWith('r1', 'family')
    expect(enrollRunnerWorkspace.mock.invocationCallOrder[0]).toBeLessThan(setRunnerOrder.mock.invocationCallOrder[0])
  })

  it("leaves someone else's box to its owner", async () => {
    getRunnerOrder.mockResolvedValue([])
    setRunnerOrder.mockResolvedValue([])
    renderPanel(fleetOf({ id: 'r2', name: 'colleague-mbp', can_manage: false }))
    await add('r2')
    await waitFor(() => expect(setRunnerOrder).toHaveBeenCalled())
    expect(enrollRunnerWorkspace).not.toHaveBeenCalled()
  })

  it('says why when the box cannot serve the workspace, and saves nothing', async () => {
    getRunnerOrder.mockResolvedValue([])
    enrollRunnerWorkspace.mockRejectedValue(new Error("this runner's owner is not an editor (or above) in 'family'"))
    renderPanel(fleetOf({ id: 'r1', name: 'cloud-ec2-2' }))
    await add('r1')
    expect(await screen.findByText(/not an editor/)).toBeTruthy()
    expect(setRunnerOrder).not.toHaveBeenCalled()
  })
})
