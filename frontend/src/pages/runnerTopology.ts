import type { TopologyAgentOut, TopologyRouteOut, TopologyRunnerOut } from '@/api/workspaces'

/** Where an agent's default list leaves it right now.
 *  - `ok`: an enabled runner that is online and allowed to claim
 *  - `down`: it has enabled runners, none of them can take a turn now
 *  - `unrouted`: no enabled runner in its default list — its turns cannot run */
export type AgentHealth = 'ok' | 'down' | 'unrouted'

export function defaultRoutes(agent: TopologyAgentOut): TopologyRouteOut[] {
  return agent.routes.filter((r) => r.source === '').sort((a, b) => a.rank - b.rank)
}

export function ruleRoutes(agent: TopologyAgentOut): TopologyRouteOut[] {
  return agent.routes.filter((r) => r.source !== '')
}

export function agentHealth(agent: TopologyAgentOut, runners: Map<string, TopologyRunnerOut>): AgentHealth {
  const enabled = defaultRoutes(agent).filter((r) => r.enabled)
  if (enabled.length === 0) return 'unrouted'
  const live = enabled.some((r) => r.can_claim && runners.get(r.runner_id)?.status === 'online')
  return live ? 'ok' : 'down'
}

/** Would this agent lose every runner it can use if `runnerId` went away?
 *  The question "what breaks if this laptop closes?" — the answer is the agents
 *  whose only live, claimable, enabled default runner is this one. */
export function dependsSolelyOn(
  agent: TopologyAgentOut,
  runnerId: string,
  runners: Map<string, TopologyRunnerOut>,
): boolean {
  const usable = defaultRoutes(agent).filter(
    (r) => r.enabled && r.can_claim && runners.get(r.runner_id)?.status === 'online',
  )
  return usable.length > 0 && usable.every((r) => r.runner_id === runnerId)
}

/** Semantic token for a runner's live status. */
export function statusTone(status: string | undefined): string {
  switch (status) {
    case 'online':
      return 'text-success'
    case 'stale':
    case 'degraded':
    case 'paused':
      return 'text-warning'
    default:
      return 'text-destructive'
  }
}
