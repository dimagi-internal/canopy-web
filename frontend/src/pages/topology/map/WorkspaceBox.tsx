import { type JSX } from 'react'
import { clsx } from 'clsx'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { type TopologyRunnerOut } from '@/api/workspaces'
import { type FleetMap, type MapAgent, type MapWorkspace } from '@/pages/topology/topologyMap'
import { addKey, DefaultOrderLine, OwnerLane } from '@/pages/topology/map/OwnerLane'
import { type Selection } from '@/pages/topology/map/shared'

export function WorkspaceBox({
  ws,
  map,
  collapsed,
  selectedAgent,
  selectedRunner,
  adding,
  orderSelected,
  onToggle,
  onSelect,
  register,
}: {
  ws: MapWorkspace
  map: FleetMap
  collapsed: Set<string>
  selectedAgent: MapAgent | undefined
  selectedRunner: TopologyRunnerOut | undefined
  adding: string | null
  orderSelected: string | null
  onToggle: (ws: string) => void
  onSelect: (s: Selection) => void
  register: (key: string) => (el: HTMLElement | null) => void
}): JSX.Element {
  const isCollapsed = collapsed.has(ws.slug)
  const Chevron = isCollapsed ? ChevronRight : ChevronDown
  const empty = ws.agentCount === 0 && ws.runnerCount === 0
  return (
    <section
      ref={register(`ws:${ws.slug}`)}
      className={clsx(
        'rounded-xl border border-border',
        ws.depth % 2 === 0 ? 'bg-card' : 'bg-background',
      )}
      // A busy workspace takes the whole row beside its siblings: squeezed into
      // one grid column its agents stack, and the arrows between neighbours
      // shrink to stubs running over the card text.
      style={ws.depth > 0 && !isCollapsed && isBusy(ws) ? { gridColumn: '1 / -1' } : undefined}
      data-testid={`map-ws-${ws.slug}`}
    >
      <h3 className="m-0">
        <button
          type="button"
          onClick={() => onToggle(ws.slug)}
          aria-expanded={!isCollapsed}
          className="flex min-h-11 w-full flex-wrap items-center gap-x-2 gap-y-1 rounded-xl px-3 py-2 text-left hover:bg-muted/50"
        >
          <Chevron className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className={clsx('font-semibold text-foreground', ws.depth === 0 ? 'text-[16px]' : 'text-[14px]')}>
            {ws.displayName}
          </span>
          <span className="text-[12px] text-muted-foreground">
            {empty
              ? 'no agents or runners'
              : `${ws.agentCount} agent${ws.agentCount === 1 ? '' : 's'} · ${ws.runnerCount} runner${ws.runnerCount === 1 ? '' : 's'}`}
          </span>
        </button>
      </h3>
      {!isCollapsed && (
        <DefaultOrderLine ws={ws} map={map} selected={orderSelected === ws.slug} onSelect={onSelect} />
      )}
      {!isCollapsed && !empty && (
        <div className="flex flex-col gap-3 px-3 pb-3">
          {ws.lanes.length > 0 && (
            <div className="flex flex-wrap gap-3">
              {ws.lanes.map((lane) => (
                <OwnerLane
                  key={lane.owner ?? '∅'}
                  workspace={ws.slug}
                  lane={lane}
                  map={map}
                  selectedAgent={selectedAgent}
                  selectedRunner={selectedRunner}
                  adding={adding === addKey(ws.slug, lane.owner ?? '')}
                  onSelect={onSelect}
                  register={register}
                />
              ))}
            </div>
          )}
          {ws.children.length > 0 && (
            <div className="grid items-start gap-3" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(min(320px, 100%), 1fr))' }}>
              {ws.children.map((c) => (
                <WorkspaceBox
                  key={c.slug}
                  ws={c}
                  map={map}
                  collapsed={collapsed}
                  selectedAgent={selectedAgent}
                  selectedRunner={selectedRunner}
                  adding={adding}
                  orderSelected={orderSelected}
                  onToggle={onToggle}
                  onSelect={onSelect}
                  register={register}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  )
}

/** Enough agents in its own lanes that it reads better at full width. */
function isBusy(ws: MapWorkspace): boolean {
  return ws.lanes.reduce((n, l) => n + l.agents.length, 0) >= 4
}
