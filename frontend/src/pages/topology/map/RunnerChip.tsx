import { type JSX } from 'react'
import { clsx } from 'clsx'
import { type TopologyRunnerOut } from '@/api/workspaces'
import { defaultRoutes, ruleRoutes, statusTone } from '@/pages/runnerTopology'
import { shortRunner, type MapAgent } from '@/pages/topology/topologyMap'
import { type Selection } from '@/pages/topology/map/shared'

export function RunnerChip({
  runner,
  selectedAgent,
  selectedRunner,
  onSelect,
  register,
}: {
  runner: TopologyRunnerOut
  selectedAgent: MapAgent | undefined
  selectedRunner: TopologyRunnerOut | undefined
  onSelect: (s: Selection) => void
  register: (key: string) => (el: HTMLElement | null) => void
}): JSX.Element {
  const isSelected = selectedRunner?.id === runner.id
  // With an agent selected: this runner's place in that agent's order, if any.
  const rank = selectedAgent?.routing
    ? defaultRoutes(selectedAgent.routing).findIndex((r) => r.runner_id === runner.id)
    : -1
  const route = rank >= 0 ? defaultRoutes(selectedAgent!.routing!)[rank] : undefined
  const ruled = selectedAgent?.routing ? ruleRoutes(selectedAgent.routing).some((r) => r.runner_id === runner.id) : false
  const dimmed = (selectedAgent && rank < 0 && !ruled) || (selectedRunner && !isSelected)
  return (
    <button
      ref={register(`runner:${runner.id}`)}
      type="button"
      onClick={() => onSelect(isSelected ? null : { kind: 'runner', id: runner.id })}
      aria-pressed={isSelected}
      title={`${runner.name} · ${runner.kind} · ${runner.status}${runner.host ? ` · ${runner.host}` : ''}`}
      className={clsx(
        'inline-flex min-h-8 items-center gap-1.5 rounded-md border px-2 py-1 text-[12px] transition-opacity',
        isSelected || rank >= 0 || ruled ? 'border-primary bg-primary/10' : 'border-border bg-card hover:bg-muted',
        dimmed && 'opacity-40',
        route && !route.enabled && 'line-through',
      )}
      data-testid={`map-runner-${runner.name}`}
    >
      {rank >= 0 && <span className="font-semibold text-primary">{rank + 1}</span>}
      <span className={statusTone(runner.status)} aria-hidden="true">●</span>
      <span className="font-mono text-foreground">{shortRunner(runner.name)}</span>
      <span className="text-muted-foreground">
        {runner.status === 'online' ? runner.kind : runner.status}
        {runner.agent_count === 0 ? ' · idle' : ''}
      </span>
    </button>
  )
}
