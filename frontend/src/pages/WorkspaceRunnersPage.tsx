import { useEffect, useMemo, useState, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'
import { clsx } from 'clsx'

import {
  getRunnerTopology,
  listWorkspacePrivateRunners,
  WorkspaceApiError,
  type PrivateRunnerUseOut,
  type RunnerTopologyOut,
  type TopologyRouteOut,
  type TopologyRunnerOut,
} from '@/api/workspaces'
import { RunnersTable } from '@/components/runners/RunnersTable'
import { relativeAge } from '@/lib/relativeAge'
import { TopologyViews } from './topology/TopologyViews'
import { agentHealth, defaultRoutes, dependsSolelyOn, ruleRoutes, statusTone } from './runnerTopology'

// THE RUNNER TOPOLOGY of this workspace and every workspace below it: which box
// each agent's turns land on, and what stops if a box goes dark.
//
// Routing is decided per agent (its Settings → Routing table), and runners are
// listed per fleet (/supervisor) — neither screen could answer "what does this
// division run on?" without opening every agent. Read-only on purpose: changing
// a route stays on the agent, where its owner edits it.
//
// Selecting a runner highlights every agent that routes to it, and marks the
// ones it is the ONLY live runner for — the agents that stop if it closes.

const HEALTH_LABEL = {
  ok: { text: 'Routed', className: 'bg-success/10 text-success border-success/30' },
  down: { text: 'No live runner', className: 'bg-destructive/10 text-destructive border-destructive/30' },
  unrouted: { text: 'Unrouted', className: 'bg-warning/10 text-warning border-warning/30' },
} as const

export function WorkspaceRunnersPage(): JSX.Element {
  const { workspace: slug = '' } = useParams()
  // Keyed so switching workspace starts from a clean slate (no stale topology,
  // selection or refusal carried across) without resetting state in an effect.
  return (
    <>
      <TopologyViews />
      <Topology key={slug} slug={slug} />
    </>
  )
}

function Topology({ slug }: { slug: string }): JSX.Element {
  const [topo, setTopo] = useState<RunnerTopologyOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)

  useEffect(() => {
    let off = false
    getRunnerTopology(slug)
      .then((t) => { if (!off) setTopo(t) })
      .catch((e: unknown) => {
        if (off) return
        if (e instanceof WorkspaceApiError && (e.status === 403 || e.status === 404)) setForbidden(true)
        else setError(e instanceof Error ? e.message : 'Could not load the runner topology')
      })
    return () => { off = true }
  }, [slug])

  const runners = useMemo(
    () => new Map((topo?.runners ?? []).map((r) => [r.id, r] as const)),
    [topo],
  )

  if (forbidden) {
    return (
      <p className="text-[13px] text-muted-foreground" data-testid="topology-forbidden">
        Only a workspace admin or owner can see the runner topology.
      </p>
    )
  }
  if (error) return <p className="text-[13px] text-destructive">{error}</p>
  if (!topo) return <p className="text-[13px] text-muted-foreground">Loading…</p>

  const agentCount = topo.workspaces.reduce((n, w) => n + w.agents.length, 0)
  const selectedRunner = selected ? runners.get(selected) : undefined

  return (
    <div className="flex flex-col gap-6" data-testid="runner-topology">
      <section>
        <h2 className="text-[15px] font-semibold text-foreground">
          Runner topology{' '}
          <span className="text-muted-foreground">
            · {topo.workspaces.length} workspace{topo.workspaces.length === 1 ? '' : 's'}, {agentCount} agent
            {agentCount === 1 ? '' : 's'}, {topo.runners.length} runner{topo.runners.length === 1 ? '' : 's'}
          </span>
        </h2>
        <p className="mt-1 max-w-3xl text-[13px] text-foreground-secondary">
          Which runner each agent&rsquo;s turns land on, across this workspace and every one below it.
          An agent tries its runners in order; a rule sends one source (email, Slack…) somewhere first.
          Open a runner for why it is in its state and what depends on it. Routes are changed on each agent, under Settings.
        </p>
      </section>

      <section>
        <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Runners</h3>
        <RunnersTable runners={topo.runners} workspace={slug} open={selected} onOpen={setSelected} />
      </section>

      <PrivateRunners slug={slug} />

      <section>
        <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          Agents and their routes
          {selectedRunner && (
            <span className="ml-2 normal-case tracking-normal text-foreground-secondary" data-testid="topology-selection">
              — highlighting {selectedRunner.name}.{' '}
              <span className="text-destructive">Bold red</span> agents stop if it goes dark.{' '}
              <button type="button" onClick={() => setSelected(null)} className="text-primary hover:underline">
                clear
              </button>
            </span>
          )}
        </h3>
        <div className="overflow-x-auto">
          <table className="w-full text-[12px]" data-testid="topology-agents">
            <thead className="text-left text-muted-foreground">
              <tr className="border-b border-border">
                <th className="py-1.5 pr-3 font-medium">Agent</th>
                <th className="py-1.5 pr-3 font-medium">In order</th>
                <th className="py-1.5 pr-3 font-medium">Rules</th>
                <th className="py-1.5 font-medium">Now</th>
              </tr>
            </thead>
            <tbody>
              {topo.workspaces.map((ws) => [
                <tr key={`ws-${ws.slug}`} className="border-b border-border bg-muted/50" data-testid={`topology-ws-${ws.slug}`}>
                  <td colSpan={4} className="py-1.5 font-medium text-foreground" style={{ paddingLeft: `${ws.depth * 16 + 4}px` }}>
                    {ws.depth > 0 && <span className="text-muted-foreground">└ </span>}
                    {ws.display_name} <span className="font-normal text-muted-foreground">{ws.slug}</span>
                    {ws.agents.length === 0 && (
                      <span className="ml-2 font-normal text-muted-foreground">no agents</span>
                    )}
                  </td>
                </tr>,
                ...ws.agents.map((a) => {
                  const health = agentHealth(a, runners)
                  const touches = selected !== null && a.routes.some((r) => r.runner_id === selected)
                  const sole = selected !== null && dependsSolelyOn(a, selected, runners)
                  return (
                    <tr
                      key={`${ws.slug}-${a.slug}`}
                      className={clsx(
                        'border-b border-border align-top',
                        selected !== null && !touches && 'opacity-40',
                      )}
                      data-testid={`topology-agent-${a.slug}`}
                    >
                      <td className="py-1.5 pr-3" style={{ paddingLeft: `${ws.depth * 16 + 16}px` }}>
                        <Link
                          to={`/w/${ws.slug}/agents/${a.slug}/settings`}
                          className={clsx('hover:underline', sole ? 'font-semibold text-destructive' : 'text-foreground')}
                        >
                          {a.name}
                        </Link>{' '}
                        <span className="text-muted-foreground">{a.turn_mode}</span>
                      </td>
                      <td className="py-1.5 pr-3">
                        <RouteChips routes={defaultRoutes(a)} runners={runners} selected={selected} ordered />
                      </td>
                      <td className="py-1.5 pr-3">
                        {ruleRoutes(a).length === 0 ? (
                          <span className="text-muted-foreground">—</span>
                        ) : (
                          <ul className="flex flex-col gap-0.5">
                            {groupRules(ruleRoutes(a)).map(([key, rows]) => (
                              <li key={key}>
                                <span className="text-foreground-secondary">
                                  {rows[0].source}
                                  {rows[0].actor ? ` from ${rows[0].actor}` : ''}
                                </span>{' '}
                                → <RouteChips routes={rows} runners={runners} selected={selected} />
                                {rows[0].strict && <span className="ml-1 text-muted-foreground">only</span>}
                                {rows[0].turn_mode && <span className="ml-1 text-muted-foreground">{rows[0].turn_mode}</span>}
                              </li>
                            ))}
                          </ul>
                        )}
                      </td>
                      <td className="py-1.5">
                        <span className={clsx('rounded border px-1.5 py-0.5', HEALTH_LABEL[health].className)}>
                          {HEALTH_LABEL[health].text}
                        </span>
                      </td>
                    </tr>
                  )
                }),
              ])}
            </tbody>
          </table>
        </div>
        <p className="mt-2 max-w-3xl text-[11px] text-muted-foreground">
          A struck-through runner is switched off for that agent. <span className="text-destructive">⚠</span> means
          the runner&rsquo;s owner is not a member of the agent&rsquo;s workspace, so it can never claim that
          agent&rsquo;s turns — re-pair it from someone who is, or route elsewhere.
        </p>
      </section>
    </div>
  )
}

function groupRules(rows: TopologyRouteOut[]): [string, TopologyRouteOut[]][] {
  const groups = new Map<string, TopologyRouteOut[]>()
  for (const r of rows) {
    const key = `${r.source}\u0000${r.actor}`
    groups.set(key, [...(groups.get(key) ?? []), r])
  }
  return [...groups.entries()].map(([k, v]) => [k, v.sort((a, b) => a.rank - b.rank)])
}

function RouteChips({
  routes,
  runners,
  selected,
  ordered = false,
}: {
  routes: TopologyRouteOut[]
  runners: Map<string, TopologyRunnerOut>
  selected: string | null
  ordered?: boolean
}): JSX.Element {
  if (routes.length === 0) return <span className="text-muted-foreground">none</span>
  return (
    <span className="inline-flex flex-wrap gap-1">
      {routes.map((r, i) => {
        const runner = runners.get(r.runner_id)
        return (
          <span
            key={`${r.runner_id}-${i}`}
            className={clsx(
              'inline-flex items-center gap-1 whitespace-nowrap rounded border px-1.5 py-0.5',
              r.runner_id === selected ? 'border-primary bg-primary/10' : 'border-border',
              !r.enabled && 'line-through opacity-60',
            )}
            title={runner?.directed
              ? 'Directed only — this runner takes only work put on it, never these routed turns'
              : r.can_claim ? undefined : "This runner's owner is not in the agent's workspace — it cannot claim these turns"}
          >
            {ordered && <span className="text-muted-foreground">{i + 1}</span>}
            <span className={statusTone(runner?.status)}>●</span>
            <span className="text-foreground">{runner?.name ?? 'unknown'}</span>
            {runner?.directed
              ? <span className="text-muted-foreground">directed only</span>
              : !r.can_claim && <span className="text-destructive">⚠</span>}
          </span>
        )
      })}
    </span>
  )
}

// People's OWN machines (canopy-web#1704): a session there runs an agent in that
// person's Claude Code and is recorded as a local turn. Listed beside the runners
// because "where does this agent's work happen" now has a second answer, but they
// are not runners — they claim nothing and nothing routes to them, so there is no
// status and no route chip. Hidden until someone has used one.
function PrivateRunners({ slug }: { slug: string }): JSX.Element | null {
  const [rows, setRows] = useState<PrivateRunnerUseOut[] | null>(null)

  useEffect(() => {
    let off = false
    listWorkspacePrivateRunners(slug)
      .then((r) => { if (!off) setRows(r) })
      .catch(() => { if (!off) setRows([]) })
    return () => { off = true }
  }, [slug])

  if (!rows || rows.length === 0) return null
  return (
    <section data-testid="private-runners">
      <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        Private runners · people running agents in their own Claude Code
      </h3>
      <table className="w-full text-[12px]">
        <tbody className="divide-y divide-border">
          {rows.map((w) => (
            <tr key={w.id} className={clsx(w.retired && 'opacity-60')}>
              <td className="py-1.5 pr-3 text-foreground">
                {w.name} <span className="text-muted-foreground">— private runner, {w.owner_name}</span>
              </td>
              <td className="py-1.5 pr-3">{w.agents.join(', ')}</td>
              <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">
                {w.local_turns} local turn{w.local_turns === 1 ? '' : 's'}
              </td>
              <td className="py-1.5 text-right text-muted-foreground">
                {w.last_seen_at ? relativeAge(w.last_seen_at) : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
