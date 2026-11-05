import { type JSX } from 'react'
import { clsx } from 'clsx'
import { type RunnerOut } from '@/api/harness'
import { AlertList } from '@/components/alerts/AlertList'
import { type AgentEdgeOut } from '@/api/workspaces'
import { edgeKey } from '@/pages/agentTopology'
import { WorkspaceOrderPanel } from '@/pages/topology/WorkspaceOrderPanel'
import { type FleetMap } from '@/pages/topology/topologyMap'
import { PanelHeading } from '@/pages/topology/panels/shared'
import { type Selection } from '@/pages/topology/map/shared'
import { AgentPanel } from '@/pages/topology/panels/AgentPanel'
import { RunnerPanel } from '@/pages/topology/panels/RunnerPanel'
import { AddRunnerPanel } from '@/pages/topology/panels/AddRunnerPanel'

export function SidePanel({
  workspace,
  map,
  fleet,
  selection,
  onRefresh,
  busy,
  error,
  onSelect,
  onGrant,
  onRevoke,
}: {
  /** The workspace this map is for — its alerts show when nothing is selected. */
  workspace: string
  map: FleetMap
  fleet: Map<string, RunnerOut>
  selection: Selection
  onRefresh: () => void
  busy: boolean
  error: string | null
  onSelect: (s: Selection) => void
  onGrant: (e: AgentEdgeOut) => void
  onRevoke: (e: AgentEdgeOut) => void
}): JSX.Element {
  return (
    <aside
      className={clsx(
        'flex min-w-0 flex-[1_1_300px] flex-col gap-4 rounded-xl border border-border bg-card p-4 text-[13px]',
        // Room for the routing table and the runner's controls once there is one.
        selection?.kind === 'agent' ? 'lg:max-w-lg' : selection ? 'lg:max-w-md' : 'lg:max-w-sm',
      )}
      data-testid="map-panel"
    >
      {selection?.kind === 'agent' && map.agents.get(selection.slug) ? (
        <AgentPanel
          agent={map.agents.get(selection.slug)!}
          map={map}
          busy={busy}
          onSelect={onSelect}
          onGrant={onGrant}
          onRevoke={onRevoke}
          onRoutingSaved={onRefresh}
        />
      ) : selection?.kind === 'runner' && map.runners.get(selection.id) ? (
        <RunnerPanel
          runner={map.runners.get(selection.id)!}
          detail={fleet.get(selection.id)}
          map={map}
          onSelect={onSelect}
        />
      ) : selection?.kind === 'order' ? (
        <WorkspaceOrderPanel
          key={selection.workspace}
          workspace={selection.workspace}
          map={map}
          fleet={fleet}
          onSaved={onRefresh}
          onSelectAgent={(slug) => onSelect({ kind: 'agent', slug })}
        />
      ) : selection?.kind === 'add' ? (
        <AddRunnerPanel workspace={selection.workspace} owner={selection.owner} map={map} />
      ) : (
        <Summary workspace={workspace} map={map} onSelect={onSelect} />
      )}
      {error && <p className="text-[12px] text-destructive">{error}</p>}
    </aside>
  )
}

function Summary({ workspace, map, onSelect }: {
  workspace: string
  map: FleetMap
  onSelect: (s: Selection) => void
}): JSX.Element {
  const confined = [...map.edges.values()].filter((e) => e.access === 'confined')
  const idle = [...map.runners.values()].filter((r) => r.agent_count === 0)
  return (
    <>
      <h3 className="m-0 text-[15px] font-semibold text-foreground">What needs attention</h3>
      {/* Problems someone can fix (an agent with no runner it can use, a dead
          credential, stuck turns…) are alerts, read like every screen reads
          them; the two lists below are plain facts about the wiring. */}
      <AlertList scope={`workspace:${workspace}`} title="Alerts here" />
      <section className="flex flex-col gap-1">
        <PanelHeading>Agents confined to read mode or connected sites by another</PanelHeading>
        {confined.length === 0 && <span className="text-success">None.</span>}
        {confined.map((e) => (
          <button key={edgeKey(e.source, e.target)} type="button" onClick={() => onSelect({ kind: 'agent', slug: e.source })} className="flex justify-between gap-2 text-left hover:underline">
            <span>{map.agents.get(e.source)?.name} → {map.agents.get(e.target)?.name}</span>
            <span className="text-[12px] text-warning">{e.capabilities.join(', ')} only</span>
          </button>
        ))}
      </section>
      {idle.length > 0 && (
        <section className="flex flex-col gap-1">
          <PanelHeading>Runners serving nobody</PanelHeading>
          {idle.map((r) => (
            <button key={r.id} type="button" onClick={() => onSelect({ kind: 'runner', id: r.id })} className="text-left font-mono hover:underline">
              {r.name}
            </button>
          ))}
        </section>
      )}
    </>
  )
}
