import { type JSX } from 'react'
import { Link } from 'react-router-dom'
import { clsx } from 'clsx'
import { type RunnerOut } from '@/api/harness'
import { RunnerFacts } from '@/pages/runner/RunnerOverview'
import { runnerHref } from '@/lib/scopedLinks'
import { type TopologyRunnerOut } from '@/api/workspaces'
import { defaultRoutes, dependsSolelyOn, statusTone } from '@/pages/runnerTopology'
import { type FleetMap } from '@/pages/topology/topologyMap'
import { PanelHeading } from '@/pages/topology/panels/shared'
import { type Selection } from '@/pages/topology/map/shared'

export function RunnerPanel({
  runner,
  detail,
  map,
  onSelect,
}: {
  runner: TopologyRunnerOut
  /** The same runner as GET /harness/runners/ serves it — absent when the
   *  viewer cannot list it (a box homed outside their workspaces). */
  detail: RunnerOut | undefined
  map: FleetMap
  onSelect: (s: Selection) => void
}): JSX.Element {
  const users = [...map.agents.values()].filter((a) => a.routing?.routes.some((r) => r.runner_id === runner.id))
  const liveLink = (
    <Link to={`/supervisor?tab=runners`} className="text-[12px] text-primary hover:underline">
      Live status →
    </Link>
  )
  return (
    <>
      {detail ? (
        <div className="flex flex-col gap-2">
          <div className="flex items-baseline justify-between gap-2">
            <h3 className="m-0 font-mono text-[16px] font-semibold text-foreground">{runner.name}</h3>
            {/* Every control for this box is on its own page; the map shows what it is
                and who routes here, and edits an agent's routing on the agent. */}
            <Link to={runnerHref(detail)} className="text-[12px] font-medium text-primary hover:underline"
              data-testid="map-runner-open">
              Open runner →
            </Link>
          </div>
          <RunnerFacts runner={detail} />
        </div>
      ) : (
        <div className="flex flex-col gap-1">
          <div className="flex items-baseline justify-between gap-2">
            <h3 className="m-0 font-mono text-[16px] font-semibold text-foreground">{runner.name}</h3>
            {liveLink}
          </div>
          <span className={clsx('text-[12px]', statusTone(runner.status))}>
            ● {runner.status}
            {!runner.ready && <span className="text-warning"> · not ready</span>}
          </span>
          <span className="text-[12px] text-muted-foreground">
            {runner.kind}
            {runner.host ? ` · ${runner.host}` : ''} · lives in {runner.workspace ?? '—'} · owned by {runner.owner_email ?? '—'}
          </span>
          <span className="text-[12px] text-muted-foreground" data-testid="map-runner-not-listed">
            You cannot configure this runner: it is not in a workspace you belong to. {runner.owner_email ?? 'Its owner'} can.
          </span>
        </div>
      )}
      <section className="flex flex-col gap-1" data-testid="map-runner-users">
        <PanelHeading>Agents that route to it</PanelHeading>
        {users.length === 0 && <span className="text-muted-foreground">None: this runner serves nobody.</span>}
        {users.map((a) => {
          const sole = a.routing ? dependsSolelyOn(a.routing, runner.id, map.runners) : false
          const pos = a.routing ? defaultRoutes(a.routing).findIndex((r) => r.runner_id === runner.id) : -1
          return (
            <div key={a.slug} className="flex items-baseline justify-between gap-2">
              <button type="button" onClick={() => onSelect({ kind: 'agent', slug: a.slug })} className={clsx('hover:underline', sole ? 'font-semibold text-destructive' : 'text-foreground-secondary')}>
                {a.name}
              </button>
              <span className="text-[12px] text-muted-foreground">
                {pos >= 0 ? `#${pos + 1} in order` : 'by a rule'}
                {sole ? ' · stops if it goes dark' : ''}
              </span>
            </div>
          )
        })}
        {users.length > 0 && (
          <span className="text-[11px] text-muted-foreground">Select an agent to change where it runs.</span>
        )}
      </section>
    </>
  )
}
