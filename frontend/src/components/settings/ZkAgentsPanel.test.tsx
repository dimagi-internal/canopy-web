// @vitest-environment jsdom
// "Your agents" on the Security card (canopy-web#1550): status only — no
// zero-knowledge switch — and ONE "Finish setup" that, with one unlock, gives
// the person their one recovery code, finishes agents and lets machines in.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const agents = vi.hoisted(() => ({ listAgents: vi.fn(), getAgentCredentialStatus: vi.fn() }))
vi.mock('@/api/agents', () => agents)
const vault = vi.hoisted(() => ({ getZkState: vi.fn() }))
vi.mock('@/api/vault', () => vault)
const ops = vi.hoisted(() => ({ prepareTurnOnZk: vi.fn(), vaultKeyForFlow: vi.fn(), ensureRecoveryCode: vi.fn(),
  grantPending: vi.fn() }))
vi.mock('@/vault/operations', () => ops)
vi.mock('@/vault/prf', async (orig) => ({ ...(await orig<typeof import('@/vault/prf')>()), browserSupportsPrf: vi.fn(async () => true) }))

const { ZkAgentsPanel } = await import('./ZkAgentsPanel')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const ready = { enabled: true, kv: 1, pending: [], sealed: [] }

function fleet() {
  agents.listAgents.mockResolvedValue({ items: [{ slug: 'hal', name: 'Hal' }, { slug: 'eva', name: 'Eva' },
    { slug: 'ada', name: 'Ada' }, { slug: 'ace', name: 'ACE' }] })
  vault.getZkState.mockImplementation(async (slug: string) => {
    if (slug === 'ace') throw new Error('403') // not mine
    if (slug === 'hal') return ready
    if (slug === 'eva') return { enabled: true, kv: 0, pending: [], sealed: ['canopy-pat'] }
    return { ...ready, pending: [{ kind: 'device', target: 'dk', runner_name: 'cloud-ec2-2' }] }
  })
  agents.getAgentCredentialStatus.mockResolvedValue([{ name: 'canopy-pat', set: true }, { name: 'x', set: false }])
}

describe('ZkAgentsPanel', () => {
  it('shows where each agent I administer stands — and no on/off switch', async () => {
    fleet()
    render(<ZkAgentsPanel />)
    expect((await screen.findByTestId('zk-agent-status-hal')).textContent).toBe('Ready')
    expect(screen.getByTestId('zk-agent-status-eva').textContent).toBe('Waiting for your key')
    expect(screen.getByTestId('zk-agent-status-ada').textContent).toBe('cloud-ec2-2 waiting')
    expect(screen.queryByTestId('zk-agent-ace')).toBeNull()
    expect(screen.queryByText(/turn off|turn on|zero-knowledge/i)).toBeNull()
    expect(screen.getByTestId('zk-agents-start').textContent).toBe('Finish setup for 2 agents')
  })

  it('says so when no agent keeps secrets here', async () => {
    agents.listAgents.mockResolvedValue({ items: [{ slug: 'ada', name: 'Ada' }] })
    vault.getZkState.mockResolvedValue({ enabled: true, kv: 0, pending: [] })
    agents.getAgentCredentialStatus.mockResolvedValue([])
    render(<ZkAgentsPanel />)
    expect(await screen.findByTestId('zk-agents-none')).toBeTruthy()
  })

  it('one unlock: the person recovery code first, then finish, then let machines in', async () => {
    fleet()
    const me = { userId: '7', scalar: new Uint8Array(32) }
    ops.vaultKeyForFlow.mockResolvedValue({ me, replaced: [] })
    let release!: () => void
    ops.ensureRecoveryCode.mockImplementation(async (_me, confirm: (c: string) => Promise<void>) => {
      await confirm('PERSON-CODE')
      release?.()
    })
    const commit = vi.fn(async () => ({}))
    ops.prepareTurnOnZk.mockImplementation(async (slug: string) => ({ slug, kv: 1, code: '', commit }))
    ops.grantPending.mockResolvedValue({})
    render(<ZkAgentsPanel />)
    fireEvent.click(await screen.findByTestId('zk-agents-start'))
    expect((await screen.findByTestId('zk-agents-code-value')).textContent).toBe('PERSON-CODE')
    expect(commit).not.toHaveBeenCalled()  // nothing changes before the code is kept
    const done = new Promise<void>((r) => (release = r))
    fireEvent.click(screen.getByTestId('zk-agents-saved'))
    fireEvent.click(screen.getByTestId('zk-agents-commit'))
    await done
    await waitFor(() => expect(ops.grantPending).toHaveBeenCalledTimes(2))
    expect(ops.vaultKeyForFlow).toHaveBeenCalledTimes(1)
    expect(ops.prepareTurnOnZk.mock.calls.map((c) => c[0])).toEqual(['eva'])
    expect(commit).toHaveBeenCalledTimes(1)
    expect(ops.grantPending.mock.calls.map((c) => c[0]).sort()).toEqual(['ada', 'eva'])
  })
})
