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

const { ZkAgentsPanel } = await import('./ZkAgentsPanel')

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
    expect((await screen.findByTestId('zk-agent-status-hal')).textContent).toBe('Private to you ✓')
    expect(screen.getByTestId('zk-agent-status-eva').textContent).toBe('Private to you ✓')
    expect(screen.getByTestId('zk-agent-status-ada').textContent).toMatch(/^cloud-ec2-2 waiting — approve it under Your machines/)
    expect(screen.getByTestId('zk-agent-status-ada').querySelector('a')!.getAttribute('href')).toBe('#your-machines')
    expect(screen.queryByTestId('zk-agent-ace')).toBeNull()
    expect(screen.queryByTestId('zk-agent-lex')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
    expect(document.body.textContent).not.toMatch(/finish|\block\b|vault key|zero-knowledge/i)
  })

  it('says so when no agent keeps secrets here', async () => {
    agents.listAgents.mockResolvedValue({ items: [{ slug: 'lex', name: 'lex', credential_source: '1password' }] })
    render(<ZkAgentsPanel />)
    expect(await screen.findByTestId('zk-agents-none')).toBeTruthy()
  })
})
