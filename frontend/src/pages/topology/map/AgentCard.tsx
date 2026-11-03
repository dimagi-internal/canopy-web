import { type JSX } from 'react'
import { clsx } from 'clsx'
import { type TopologyRunnerOut } from '@/api/workspaces'
import { edgeKey } from '@/pages/agentTopology'
import { defaultRoutes, dependsSolelyOn, ruleRoutes } from '@/pages/runnerTopology'
import { shortRunner, type FleetMap, type MapAgent } from '@/pages/topology/topologyMap'
import { HEALTH, type Selection } from '@/pages/topology/map/shared'

export function AgentCard({
  agent,
  map,
  selectedAgent,
  selectedRunner,
  onSelect,
  register,
}: {
  agent: MapAgent
  map: FleetMap
  selectedAgent: MapAgent | undefined
  selectedRunner: TopologyRunnerOut | undefined
  onSelect: (s: Selection) => void
  register: (key: string) => (el: HTMLElement | null) => void
}): JSX.Element {
  const isSelected = selectedAgent?.slug === agent.slug
  const edge = selectedAgent && !isSelected ? map.edges.get(edgeKey(selectedAgent.slug, agent.slug)) : undefined
  const usesRunner = selectedRunner && agent.routing?.routes.some((r) => r.runner_id === selectedRunner.id)
  const sole = selectedRunner && agent.routing ? dependsSolelyOn(agent.routing, selectedRunner.id, map.runners) : false
  const dimmed = (selectedAgent && !isSelected && (!edge || edge.access === 'none')) || (selectedRunner && !usesRunner)
  const order = agent.routing ? defaultRoutes(agent.routing).filter((r) => r.enabled) : []
  const rules = agent.routing ? ruleRoutes(agent.routing).length : 0
  const health = HEALTH[agent.health]
  return (
    <button
      ref={register(`agent:${agent.slug}`)}
      type="button"
      onClick={() => onSelect(isSelected ? null : { kind: 'agent', slug: agent.slug })}
      aria-pressed={isSelected}
      className={clsx(
        'flex min-h-11 w-[200px] max-w-full flex-col items-start gap-0.5 rounded-lg border px-2.5 py-2 text-left transition-opacity',
        isSelected && 'border-primary bg-primary/10 ring-2 ring-primary/30',
        !isSelected && edge?.access === 'full' && 'border-success bg-card',
        !isSelected && edge?.access === 'confined' && 'border-warning bg-card',
        !isSelected && !edge && (sole ? 'border-destructive bg-card' : usesRunner ? 'border-primary bg-card' : 'border-border bg-card hover:bg-muted'),
        dimmed && 'opacity-40',
      )}
      data-testid={`map-agent-${agent.slug}`}
    >
      <span className="flex w-full items-baseline justify-between gap-2">
        <span className="text-[13px] font-semibold text-foreground">{agent.name}</span>
        <span className="text-[11px] text-muted-foreground">{agent.turnMode}</span>
      </span>
      <span className="text-[11px] text-muted-foreground">
        {order.length
          ? `runs on ${order.map((r) => shortRunner(map.runners.get(r.runner_id)?.name ?? '?')).join(' → ')}`
          : 'no runner'}
        {rules ? ` + ${rules} rule${rules === 1 ? '' : 's'}` : ''}
        {agent.follows ? ` · ${agent.follows.workspace} default` : ''}
      </span>
      {agent.interfacePublished && <span className="text-[11px] text-info">interface published</span>}
      {health && <span className={clsx('text-[11px]', health.className)}>{health.text}</span>}
      {sole && <span className="text-[11px] font-semibold text-destructive">stops if it goes dark</span>}
    </button>
  )
}

// ---- the side panel ----------------------------------------------------------
