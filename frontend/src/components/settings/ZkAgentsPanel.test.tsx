// @vitest-environment jsdom
// Zero-knowledge for several agents at once (canopy-web#1550): one unlock,
// every recovery code shown together, nothing enabled until they are kept.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const agents = vi.hoisted(() => ({ listAgents: vi.fn(), getAgentCredentialStatus: vi.fn() }))
vi.mock('@/api/agents', () => agents)
const vault = vi.hoisted(() => ({ getZkState: vi.fn() }))
vi.mock('@/api/vault', () => vault)
const ops = vi.hoisted(() => ({ prepareTurnOnZk: vi.fn(), vaultKeyForFlow: vi.fn() }))
vi.mock('@/vault/operations', () => ops)
vi.mock('@/vault/prf', async (orig) => ({ ...(await orig<typeof import('@/vault/prf')>()), browserSupportsPrf: vi.fn(async () => true) }))

const { ZkAgentsPanel } = await import('./ZkAgentsPanel')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function fleet() {
  agents.listAgents.mockResolvedValue({ items: [{ slug: 'hal', name: 'Hal' }, { slug: 'eva', name: 'Eva' },
    { slug: 'ada', name: 'Ada' }, { slug: 'ace', name: 'ACE' }] })
  vault.getZkState.mockImplementation(async (slug: string) => {
    if (slug === 'ace') throw new Error('403') // not mine
    return { enabled: slug === 'ada' }
  })
  agents.getAgentCredentialStatus.mockResolvedValue([{ name: 'canopy-pat', set: true }, { name: 'x', set: false }])
}

describe('ZkAgentsPanel', () => {
  it('lists only agents I administer with zero-knowledge off, and is hidden when there are none', async () => {
    agents.listAgents.mockResolvedValue({ items: [{ slug: 'ada', name: 'Ada' }] })
    vault.getZkState.mockResolvedValue({ enabled: true })
    const { container } = render(<ZkAgentsPanel />)
    await waitFor(() => expect(vault.getZkState).toHaveBeenCalled())
    expect(container.innerHTML).toBe('')
  })

  it('one unlock prepares every picked agent; codes show together; enabling waits for them to be kept', async () => {
    fleet()
    const me = { userId: '7' }
    ops.vaultKeyForFlow.mockResolvedValue({ me, replaced: [] })
    const commits = { hal: vi.fn(async () => ({})), eva: vi.fn(async () => ({})) }
    ops.prepareTurnOnZk.mockImplementation(async (slug: 'hal' | 'eva') => ({ slug, kv: 1, code: `CODE-${slug}`, commit: commits[slug] }))
    render(<ZkAgentsPanel />)
    fireEvent.click(await screen.findByText('Turn on zero-knowledge for all agents'))
    const codes = await screen.findByTestId('zk-agents-codes')
    expect(codes.textContent).toContain('CODE-hal')
    expect(codes.textContent).toContain('CODE-eva')
    expect(ops.vaultKeyForFlow).toHaveBeenCalledTimes(1)
    expect(ops.prepareTurnOnZk.mock.calls.map((c) => [c[0], c[1]])).toEqual([['hal', ['canopy-pat']], ['eva', ['canopy-pat']]])
    expect(commits.hal).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('zk-agents-saved'))
    fireEvent.click(screen.getByTestId('zk-agents-commit'))
    expect((await screen.findByTestId('zk-agents-done')).textContent).toContain('hal, eva')
    expect(commits.hal).toHaveBeenCalled()
    expect(commits.eva).toHaveBeenCalled()
  })

  it('unpicked agents are left alone', async () => {
    fleet()
    ops.vaultKeyForFlow.mockResolvedValue({ me: {}, replaced: [] })
    ops.prepareTurnOnZk.mockImplementation(async (slug: string) => ({ slug, kv: 1, code: 'C', commit: vi.fn() }))
    render(<ZkAgentsPanel />)
    fireEvent.click(await screen.findByTestId('zk-agents-pick-eva'))
    fireEvent.click(screen.getByTestId('zk-agents-start'))
    await screen.findByTestId('zk-agents-codes')
    expect(ops.prepareTurnOnZk.mock.calls.map((c) => c[0])).toEqual(['hal'])
  })
})
