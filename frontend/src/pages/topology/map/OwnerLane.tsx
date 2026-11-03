import { type JSX } from 'react'
import { clsx } from 'clsx'
import { Plus } from 'lucide-react'
import { type TopologyRunnerOut } from '@/api/workspaces'
import {
  initials,
  shortRunner,
  type FleetMap,
  type Lane,
  type MapAgent,
  type MapWorkspace,
} from '@/pages/topology/topologyMap'
import { type Selection } from '@/pages/topology/map/shared'
import { RunnerChip } from '@/pages/topology/map/RunnerChip'
import { AgentCard } from '@/pages/topology/map/AgentCard'

export function addKey(workspace: string, owner: string): string {
  return `${workspace}\u0000${owner}`
}

/** The workspace's default runners, one line, at the top of its box — the
 *  order every agent here without its own follows. Selecting it opens the editor. */
export function DefaultOrderLine({
  ws,
  map,
  selected,
  onSelect,
}: {
  ws: MapWorkspace
  map: FleetMap
  selected: boolean
  onSelect: (s: Selection) => void
}): JSX.Element {
  const from = map.orderFrom.get(ws.slug) ?? null
  const names = ws.order.map((id) => shortRunner(map.runners.get(id)?.name ?? '?'))
  return (
    <div className="px-3 pb-2">
      <button
        type="button"
        onClick={() => onSelect(selected ? null : { kind: 'order', workspace: ws.slug })}
        aria-pressed={selected}
        className={clsx(
          'inline-flex min-h-8 flex-wrap items-center gap-1.5 rounded-md border px-2 py-1 text-left text-[12px]',
          selected ? 'border-primary bg-primary/10' : 'border-dashed border-border hover:bg-muted',
        )}
        data-testid={`map-order-${ws.slug}`}
      >
        <span className="text-muted-foreground">Default runners:</span>
        {names.length > 0 ? (
          <span className="font-mono text-foreground">{names.join(' → ')}</span>
        ) : from ? (
          <span className="text-foreground-secondary">follows {from}&rsquo;s</span>
        ) : (
          <span className="text-warning">none set</span>
        )}
      </button>
    </div>
  )
}

/** Grow by agent count, and ask for room for up to four cards side by side
 *  (each 200px + gap) before wrapping onto a row of its own. */
function laneFlex(lane: Lane): string {
  const cards = Math.max(1, Math.min(lane.agents.length, 4))
  return `${cards} 1 ${cards * 208 + 22}px`
}

export function OwnerLane({
  workspace,
  lane,
  map,
  selectedAgent,
  selectedRunner,
  adding,
  onSelect,
  register,
}: {
  workspace: string
  lane: Lane
  map: FleetMap
  selectedAgent: MapAgent | undefined
  selectedRunner: TopologyRunnerOut | undefined
  adding: boolean
  onSelect: (s: Selection) => void
  register: (key: string) => (el: HTMLElement | null) => void
}): JSX.Element {
  return (
    <div
      className={clsx(
        'flex min-w-0 flex-col gap-2 rounded-lg border p-2.5',
        lane.owner ? 'border-border bg-muted/30' : 'border-dashed border-border',
      )}
      // Width in proportion to what the lane holds: equal shares put a
      // five-agent lane beside two near-empty ones and stacked its cards.
      style={{ flex: laneFlex(lane) }}
      data-testid={`lane-${lane.owner ?? 'none'}`}
    >
      <div className="flex items-center gap-2 text-[12px]">
        <span className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary/15 text-[10px] font-semibold text-primary">
          {initials(lane.owner)}
        </span>
        <span className="truncate text-foreground-secondary">{lane.owner ?? 'No owner set'}</span>
      </div>
      {(lane.runners.length > 0 || lane.owner) && (
        <div className="flex flex-wrap gap-1.5">
          {lane.runners.map((r) => (
            <RunnerChip key={r.id} runner={r} selectedAgent={selectedAgent} selectedRunner={selectedRunner} onSelect={onSelect} register={register} />
          ))}
          {/* A runner always has an owner, so the "No owner set" lane has no
              one to add a box for. */}
          {lane.owner && (
            <button
              type="button"
              onClick={() => onSelect(adding ? null : { kind: 'add', workspace, owner: lane.owner! })}
              aria-pressed={adding}
              className={clsx(
                'inline-flex min-h-8 items-center gap-1 rounded-md border border-dashed px-2 py-1 text-[12px]',
                adding ? 'border-primary bg-primary/10 text-primary' : 'border-border text-muted-foreground hover:bg-muted hover:text-foreground',
              )}
              data-testid={`map-add-runner-${workspace}-${lane.owner}`}
            >
              <Plus className="h-3.5 w-3.5" aria-hidden="true" />
              Add a runner
            </button>
          )}
        </div>
      )}
      {lane.agents.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {lane.agents.map((a) => (
            <AgentCard key={a.slug} agent={a} map={map} selectedAgent={selectedAgent} selectedRunner={selectedRunner} onSelect={onSelect} register={register} />
          ))}
        </div>
      )}
    </div>
  )
}
