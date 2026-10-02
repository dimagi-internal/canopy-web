import { describe, expect, it } from 'vitest'

import type { TopologyAgentOut, TopologyRouteOut, TopologyRunnerOut } from '@/api/workspaces'
import { agentHealth, dependsSolelyOn } from './runnerTopology'

const runner = (id: string, status: string) => ({ id, status }) as TopologyRunnerOut
const route = (runner_id: string, rank: number, extra: Partial<TopologyRouteOut> = {}): TopologyRouteOut => ({
  runner_id, rank, enabled: true, source: '', actor: '', strict: false, turn_mode: '', can_claim: true, ...extra,
})
const agent = (routes: TopologyRouteOut[]) => ({ slug: 'hal', name: 'Hal', turn_mode: 'manual', routes }) as TopologyAgentOut
const fleet = new Map([
  ['up', runner('up', 'online')],
  ['up2', runner('up2', 'online')],
  ['gone', runner('gone', 'disconnected')],
])

describe('agentHealth', () => {
  it('is unrouted with no enabled default runner, even when a rule exists', () => {
    expect(agentHealth(agent([route('up', 0, { enabled: false }), route('up', 0, { source: 'email' })]), fleet)).toBe('unrouted')
  })
  it('is down when every enabled runner is offline or cannot claim', () => {
    expect(agentHealth(agent([route('gone', 0), route('up', 1, { can_claim: false })]), fleet)).toBe('down')
  })
  it('is ok with one live claimable runner anywhere in the order', () => {
    expect(agentHealth(agent([route('gone', 0), route('up', 1)]), fleet)).toBe('ok')
  })
})

describe('dependsSolelyOn', () => {
  it('is true only when the runner is the agent\'s last live way to run', () => {
    expect(dependsSolelyOn(agent([route('up', 0), route('gone', 1)]), 'up', fleet)).toBe(true)
    expect(dependsSolelyOn(agent([route('up', 0), route('up2', 1)]), 'up', fleet)).toBe(false)
    expect(dependsSolelyOn(agent([route('gone', 0)]), 'gone', fleet)).toBe(false)
  })
})
