// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

const { getAgentCapabilities, setCapabilityGrant, revokeCapabilityGrant, role } = vi.hoisted(() => ({
  getAgentCapabilities: vi.fn(),
  setCapabilityGrant: vi.fn(),
  revokeCapabilityGrant: vi.fn(),
  role: { current: 'admin' as string | null },
}))
vi.mock('@/api/capabilities', () => ({ getAgentCapabilities, setCapabilityGrant, revokeCapabilityGrant }))
vi.mock('@/workspace/WorkspaceProvider', () => ({ useWorkspaceRole: () => role.current }))
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useOutletContext: () => ({ agent: { slug: 'ace', name: 'ACE', workspace: 'connect' } }),
}))

const { AgentCapabilitiesSection } = await import('./AgentCapabilitiesSection')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  role.current = 'admin'
})

const now = new Date().toISOString()
const lease = {
  lease_id: 'L1', agent: 'ace', capability: 'test-worker', status: 'ready', ready: true, position: null,
  instance_id: null, params: { slot: 'worker-2' }, turn_id: 'abcdef1234567890', created_at: now,
  provisioning_at: null, ready_at: now, ended_at: null, expires_at: null, last_heartbeat_at: now,
  idle_release_s: 900, reason: '', ready_info: { slot: 'worker-2' }, slot: 'worker-2', out_prefix: '',
}
const pool = (grant: unknown = null, leases: unknown[] = []) => ({
  name: 'test-worker', kind: 'pool', description: 'Shared test-worker logins', region: '', stack: '',
  manifest: { name: 'test-worker', kind: 'pool', slots: ['worker-1', 'worker-2'],
    limits: { max_concurrent: 2, max_lease_s: 7200, idle_release_s: 900 } },
  stack_outputs: {}, published_at: now, grant,
  limits: { max_concurrent: 2, max_lease_s: 7200, idle_release_s: 900 }, live_leases: leases,
})
const ec2 = {
  name: 'android-emulator', kind: 'ec2-ephemeral', description: 'An emulator box', region: 'us-east-1',
  stack: 'ace-emulator', manifest: { limits: { max_concurrent: 4, max_lease_s: 3600, idle_release_s: 600 } },
  stack_outputs: {}, published_at: now,
  grant: { id: 7, workspace: 'connect', agent: 'ace', capability: 'android-emulator', max_concurrent: 2,
    max_lease_s: null, created_by: 'jon@dimagi.com', created_at: now, updated_at: now },
  limits: { max_concurrent: 2, max_lease_s: 3600, idle_release_s: 600 }, live_leases: [],
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/w/connect/agents/ace/capabilities']}>
      <Routes>
        <Route path="/w/:workspace/agents/:slug/capabilities" element={<AgentCapabilitiesSection />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('AgentCapabilitiesSection', () => {
  it('shows each capability, its grant state, slots or stack, live leases and retired names', async () => {
    getAgentCapabilities.mockResolvedValue({
      agent: 'ace', capabilities: [pool(null, [lease]), ec2], retired: ['old-box'],
    })
    renderPage()
    const row = await screen.findByTestId('capability-test-worker')
    expect(getAgentCapabilities).toHaveBeenCalledWith('ace')
    expect(within(row).getByText('pool')).toBeTruthy()
    expect(within(row).getByText(/2 slots: worker-1, worker-2/)).toBeTruthy()
    expect(within(row).getByText('Not granted')).toBeTruthy()
    expect(within(row).getByText(/2 at once · 2h a lease · released after 15m idle/)).toBeTruthy()

    const box = screen.getByTestId('capability-android-emulator')
    expect(within(box).getByText('us-east-1')).toBeTruthy()
    expect(within(box).getByText('ace-emulator')).toBeTruthy()
    expect(within(box).getByText('Granted')).toBeTruthy()
    expect(within(box).getByText(/by jon@dimagi.com/)).toBeTruthy()
    expect(within(box).getByText(/lowered to 2 at once/)).toBeTruthy()
    expect(within(box).getByText(/manifest: 4 at once/)).toBeTruthy()

    const l = screen.getByTestId('lease-L1')
    expect(within(l).getByText('worker-2')).toBeTruthy()
    expect(within(l).getByText('turn abcdef12')).toBeTruthy()

    const retired = screen.getByTestId('retired-capabilities')
    expect(retired.tagName).toBe('DETAILS')
    expect((retired as HTMLDetailsElement).open).toBe(false)
    expect(within(retired).getByText('old-box')).toBeTruthy()
  })

  it('a non-admin sees read-only state and who grants it', async () => {
    role.current = 'editor'
    getAgentCapabilities.mockResolvedValue({ agent: 'ace', capabilities: [pool(), ec2], retired: [] })
    renderPage()
    await screen.findByTestId('capability-test-worker')
    expect(screen.getByTestId('capabilities-readonly').textContent).toMatch(/workspace admin/)
    expect(screen.queryByRole('button', { name: 'Grant' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Revoke' })).toBeNull()
  })

  it('an admin grants with optional lowered limits, then the page reloads', async () => {
    getAgentCapabilities.mockResolvedValue({ agent: 'ace', capabilities: [pool()], retired: [] })
    setCapabilityGrant.mockResolvedValue({})
    renderPage()
    const row = await screen.findByTestId('capability-test-worker')
    fireEvent.click(within(row).getByRole('button', { name: 'Grant' }))
    const form = screen.getByRole('form', { name: 'Grant test-worker' })
    fireEvent.change(within(form).getByLabelText(/Max at once/), { target: { value: '1' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Grant' }))
    await waitFor(() =>
      expect(setCapabilityGrant).toHaveBeenCalledWith('ace', 'test-worker', { max_concurrent: 1, max_lease_s: null }),
    )
    await waitFor(() => expect(getAgentCapabilities).toHaveBeenCalledTimes(2))
  })

  it("surfaces the server's refusal text verbatim", async () => {
    getAgentCapabilities.mockResolvedValue({ agent: 'ace', capabilities: [pool()], retired: [] })
    setCapabilityGrant.mockRejectedValue(
      new Error("max_concurrent 5 is above the manifest's 2; a grant may only lower it"),
    )
    renderPage()
    fireEvent.click(within(await screen.findByTestId('capability-test-worker')).getByRole('button', { name: 'Grant' }))
    const form = screen.getByRole('form', { name: 'Grant test-worker' })
    fireEvent.change(within(form).getByLabelText(/Max at once/), { target: { value: '5' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Grant' }))
    expect((await screen.findByRole('alert')).textContent).toBe(
      "max_concurrent 5 is above the manifest's 2; a grant may only lower it",
    )
  })

  it('an admin revokes after confirming', async () => {
    getAgentCapabilities.mockResolvedValue({ agent: 'ace', capabilities: [ec2], retired: [] })
    revokeCapabilityGrant.mockResolvedValue(undefined)
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderPage()
    fireEvent.click(within(await screen.findByTestId('capability-android-emulator')).getByRole('button', { name: 'Revoke' }))
    await waitFor(() => expect(revokeCapabilityGrant).toHaveBeenCalledWith(7))
    expect(confirm).toHaveBeenCalled()
    confirm.mockRestore()
  })
})
