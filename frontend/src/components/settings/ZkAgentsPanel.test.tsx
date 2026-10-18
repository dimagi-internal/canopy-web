// @vitest-environment jsdom
// "Your agents" on the Security card (canopy-web#1550): status only. Keeping an
// agent's secrets private is never a per-agent step, so there is no button —
// only who is waiting, and where to let them in.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

const agents = vi.hoisted(() => ({ listAgents: vi.fn() }))
vi.mock('@/api/agents', () => agents)
const vault = vi.hoisted(() => ({ getZkState: vi.fn() }))
vi.mock('@/api/vault', () => vault)
const deviceKeys = vi.hoisted(() => ({ listRunnerDeviceKeys: vi.fn(async () => [] as unknown[]) }))
vi.mock('@/api/deviceKeys', () => deviceKeys)

const { ZkAgentsPanel } = await import('./ZkAgentsPanel')
const { AuthContext } = await import('@/auth/AuthProvider')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const cw = 'canopy-web'

describe('ZkAgentsPanel', () => {
  it('shows each vault agent as private, names who is waiting, and has no buttons', async () => {
    agents.listAgents.mockResolvedValue({ items: [{ slug: 'hal', name: 'Hal', credential_source: cw },
      { slug: 'eva', name: 'Eva', credential_source: cw }, { slug: 'ada', name: 'Ada', credential_source: cw },
      { slug: 'ace', name: 'ACE', credential_source: cw },
      // A 1Password agent I administer: nothing of it lives here.
      { slug: 'lex', name: 'lex', credential_source: '1password' }] })
    vault.getZkState.mockImplementation(async (slug: string) => {
      if (slug === 'ace') throw new Error('403') // not mine
      if (slug === 'hal') return { enabled: true, kv: 1, pending: [], sealed: [] }
      // Born (kv 0): every eligible principal is "pending" before a key exists — nobody is waiting.
      if (slug === 'eva') return { enabled: true, kv: 0, pending: [{ kind: 'device', target: 'dk', runner_name: 'cloud-ec2-2' }], sealed: ['gog-token'] }
      return { enabled: true, kv: 1, pending: [{ kind: 'device', target: 'dk', runner_name: 'cloud-ec2-2' }] }
    })
    render(<ZkAgentsPanel />)
    expect((await screen.findByTestId('zk-agent-status-hal')).textContent).toBe('Private ✓')
    expect(screen.getByTestId('zk-agent-status-eva').textContent).toBe('Private ✓')
    expect(screen.getByTestId('zk-agent-status-ada').textContent).toMatch(/^cloud-ec2-2 waiting — approve it under Your machines/)
    expect(screen.getByTestId('zk-agent-status-ada').querySelector('a')!.getAttribute('href')).toBe('#your-machines')
    expect(screen.queryByTestId('zk-agent-ace')).toBeNull()
    expect(screen.queryByTestId('zk-agent-lex')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
    expect(document.body.textContent).not.toMatch(/finish|\block\b|vault key|zero-knowledge/i)
  })

  it('says who can open each agent, in plain words', async () => {
    agents.listAgents.mockResolvedValue({ items: [{ slug: 'hal', name: 'Hal', credential_source: cw },
      { slug: 'eva', name: 'Eva', credential_source: cw }] })
    const w = (kind: string, target: string | null, label: string, current = true) => ({ kind, kv: 1, target, label, current })
    vault.getZkState.mockImplementation(async (slug: string) => slug === 'eva'
      ? { enabled: true, kv: 0, wraps: [], pending: [], sealed: [] }
      : { enabled: true, kv: 1, pending: [], sealed: [], wraps: [
        w('recovery', null, ''), w('member', '7', 'jj@dimagi.com'), w('member', '9', 'gone@dimagi.com', false),
        w('device', 'dk1', 'acedimagi-mbp-cdp · Jonathans-MBP (Darwin)'),
        w('device', 'dk2', 'cloud-ec2-2 · cloud-ec2-2 (EC2 instance role)'),
        { ...w('device', 'dk0', 'old · old'), kv: 0 }] })
    deviceKeys.listRunnerDeviceKeys.mockResolvedValue([{ id: 'dk1', runner_name: 'acedimagi-mbp-cdp',
      runner_host: 'acedimagi@Jonathans-MacBook-Pro.local', runner_kind: 'emdash', kind: 'secure-enclave',
      ecdh_store: '', can_open: [] }])
    render(
      <AuthContext.Provider value={{ status: 'authenticated', user: { email: 'jj@dimagi.com' } as never }}>
        <ZkAgentsPanel />
      </AuthContext.Provider>,
    )
    await screen.findByTestId('zk-agent-holders-eva')
    await vi.waitFor(() => expect(screen.getByTestId('zk-agent-holders-hal').textContent)
      .toBe(' — can be opened by: you, MacBook (acedimagi), cloud-ec2-2'))
    // No key of its own yet: each secret is sealed to you and approved machines as it arrives.
    expect(screen.getByTestId('zk-agent-holders-eva').textContent).toBe(' — can be opened by: you and the machines you approve')
  })

  it('says so when no agent keeps secrets here', async () => {
    agents.listAgents.mockResolvedValue({ items: [{ slug: 'lex', name: 'lex', credential_source: '1password' }] })
    render(<ZkAgentsPanel />)
    expect(await screen.findByTestId('zk-agents-none')).toBeTruthy()
  })
})
