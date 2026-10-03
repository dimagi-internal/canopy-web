// @vitest-environment jsdom
//
// An agent with no runners of its own follows its workspace's default order
// (2026-10-03). The row must say so — routing nobody can see on the agent is the
// failure that design had to avoid — and offer the two ways out of it.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import type { AgentDefaultOrderOut } from '@/api/agents'

const getAgentDefaultOrder = vi.fn<(slug: string, ws?: string) => Promise<AgentDefaultOrderOut>>()
const putAgentRunners = vi.fn()
vi.mock('@/api/agents', () => ({ getAgentDefaultOrder, putAgentRunners }))
vi.mock('@/components/agents/RunnerAssignments', () => ({ RunnerAssignments: () => <div>own-runners</div> }))

const { AgentDefaultRunners } = await import('./AgentDefaultRunners')

const runner = (name: string, i: number) => ({
  runner_id: `id-${name}`, runner_name: name, kind: 'emdash', rank: i, online: true, ready: true, enabled: true,
})

function renderIt() {
  return render(
    <MemoryRouter>
      <AgentDefaultRunners agentSlug="jarvis" agentName="Jarvis" workspace="connect" fleet={[]} />
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentDefaultRunners', () => {
  it('shows the order it follows, and the laptop it skips for want of the repo', async () => {
    getAgentDefaultOrder.mockResolvedValue({
      own: false, workspace: 'dimagi', runners: [runner('hal-mbp-cdp', 0), runner('cloud-ec2-1', 1)],
      missing_repo: ['jj-mbp-cdp'], cannot_hold: [], repo_url: 'https://github.com/dimagi-internal/jarvis',
    })
    renderIt()
    await screen.findByTestId('default-runners-follows')
    expect(screen.getByText(/dimagi.s default order/)).toBeTruthy()
    expect(screen.getByTestId('default-runners-missing-repo').textContent).toContain('jj-mbp-cdp')
    fireEvent.click(screen.getByTestId('default-runners-get-repo'))
    expect((await screen.findByTestId('repo-setup-command')).textContent).toBe(
      'git clone https://github.com/dimagi-internal/jarvis ~/emdash-projects/jarvis',
    )
  })

  it('"Give it its own" copies the followed order onto the agent', async () => {
    getAgentDefaultOrder.mockResolvedValue({
      own: false, workspace: 'dimagi', runners: [runner('hal-mbp-cdp', 0)], missing_repo: [], cannot_hold: [], repo_url: '',
    })
    putAgentRunners.mockResolvedValue([])
    renderIt()
    fireEvent.click(await screen.findByTestId('default-runners-own-copy'))
    await waitFor(() =>
      expect(putAgentRunners).toHaveBeenCalledWith('jarvis', [{ runnerId: 'id-hal-mbp-cdp', enabled: true }], 'connect'),
    )
  })

  it('an agent with its own runners can go back to following, after a confirm', async () => {
    getAgentDefaultOrder.mockResolvedValue({ own: true, workspace: 'dimagi', runners: [], missing_repo: [], cannot_hold: [], repo_url: '' })
    putAgentRunners.mockResolvedValue([])
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderIt()
    expect(await screen.findByText('own-runners')).toBeTruthy()
    fireEvent.click(screen.getByTestId('default-runners-follow'))
    await waitFor(() => expect(putAgentRunners).toHaveBeenCalledWith('jarvis', [], 'connect'))
  })
})
