import { Fragment, useState, type JSX } from 'react'
import { Link } from 'react-router-dom'
import { clsx } from 'clsx'
import { ChevronDown, ChevronRight } from 'lucide-react'

import type { TopologyRunnerOut } from '@/api/workspaces'
import { relativeTime } from '@/components/activity/turnLog'
import { statusTone } from '@/pages/runnerTopology'
import { MissingSessions } from './MissingSessions'

// THE RUNNER TABLE — one table for Health and Topology → Runners, over the
// topology's own runner rows (`harness.topology.build`). Facts and states only:
// what each box reports, never a verdict. Every row opens its detail: each
// column spelled out, with where to change it.

type Props = {
  runners: readonly TopologyRunnerOut[]
  workspace: string
  // Controlled by a page that also reacts to the open row (Topology highlights
  // the agents that route to it); uncontrolled otherwise.
  open?: string | null
  onOpen?: (id: string | null) => void
}

const stop = (e: { stopPropagation: () => void }) => e.stopPropagation()

export function RunnersTable({ runners, workspace, open: openProp, onOpen }: Props): JSX.Element {
  const now = new Date()
  const [own, setOwn] = useState<string | null>(null)
  const open = openProp === undefined ? own : openProp
  const toggle = (id: string) => (onOpen ?? setOwn)(open === id ? null : id)
  if (runners.length === 0) return <p className="py-3 text-[12px] text-muted-foreground">No runner lives in or serves this tree.</p>
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[960px] text-[12px] [&_td]:pr-3 [&_th]:pr-3" data-testid="runners-table">
        <thead className="text-left text-muted-foreground">
          <tr><th className="py-1 font-medium">Runner</th><th className="font-medium">Owner</th>
            <th className="font-medium">Status</th><th className="font-medium" title="Running the code the deployed server expects">Code</th>
            <th className="font-medium">Its own checks</th><th className="font-medium text-right">Agents</th>
            <th className="font-medium" title="Sessions in repos canopy knows that this box has and canopy does not">Missing sessions</th>
            <th className="font-medium">Last heartbeat</th></tr>
        </thead>
        <tbody className="divide-y divide-border">
          {runners.map((r) => (
            <Fragment key={r.id}>
              <tr data-runner={r.name} aria-selected={open === r.id}
                  className={clsx('cursor-pointer hover:bg-muted/50', open === r.id && 'bg-primary/10')}
                  onClick={() => toggle(r.id)}>
                <td className="py-1.5">
                  <button type="button" aria-expanded={open === r.id}
                          className="inline-flex items-center gap-1 font-mono text-foreground">
                    {open === r.id
                      ? <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
                      : <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />}
                    {r.name}
                  </button>
                  <span className="ml-1 text-muted-foreground">{r.kind}{!r.in_tree && ' · elsewhere'}</span>
                  {r.directed && (
                    <span className="ml-1 text-[10px] text-muted-foreground" data-testid={`runner-directed-${r.name}`}
                          title="Takes only turns pinned to it and chats started on it — never routed work">
                      directed only
                    </span>
                  )}
                  {r.flags.map((f) => (
                    <span key={f} className="ml-1 rounded border border-border px-1 text-[10px] uppercase text-muted-foreground">{f}</span>
                  ))}
                </td>
                <td className="text-foreground-secondary">
                  {r.owner_email ? <a href={`mailto:${r.owner_email}`} onClick={stop} className="hover:underline">{r.owner_email}</a> : '—'}
                </td>
                <td className="whitespace-nowrap">
                  <span className={statusTone(r.status)}>{r.status}</span>
                  {r.paused && r.status !== 'paused' && <span className="text-muted-foreground"> · paused</span>}
                  {!r.ready && <span className="text-muted-foreground"> · not ready</span>}
                </td>
                <td className="whitespace-nowrap text-foreground-secondary">
                  {r.code_current === true ? 'current' : r.code_current === false ? <span className="text-warning">update pending</span> : 'unknown'}
                </td>
                <td className="text-foreground-secondary">
                  {r.problems.length === 0 ? 'all ok' : r.problems.map((p) => (
                    <span key={p.name} className={clsx('mr-2', p.status === 'fail' ? 'text-destructive' : 'text-warning')}>{p.name}</span>
                  ))}
                </td>
                <td className="text-right tabular-nums">{r.agent_count}</td>
                <td className="whitespace-nowrap">
                  <span className={clsx('tabular-nums', r.missing_sessions ? 'text-primary' : 'text-foreground-secondary')}>{r.missing_sessions}</span>
                  {!r.usage_reported_at && <span className="text-muted-foreground"> · no report</span>}
                </td>
                <td className="text-foreground-secondary">{r.last_heartbeat_at ? relativeTime(r.last_heartbeat_at, now) : 'never'}</td>
              </tr>
              {open === r.id && (
                <tr><td colSpan={8} className="bg-muted/30 p-3"><RunnerDetail runner={r} workspace={workspace} /></td></tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** One runner, each column spelled out: its status in its own words and the fix
 *  for a known cause, the code it runs against what the server expects, every
 *  check that is not ok with its detail, the agents it serves, and its usage
 *  report (with the missing-session review for its admins). */
function RunnerDetail({ runner: r, workspace }: { runner: TopologyRunnerOut; workspace: string }): JSX.Element {
  const now = new Date()
  const short = (sha: string) => sha ? sha.slice(0, 8) : 'unknown'
  return (
    <div className="grid gap-3 text-[12px] lg:grid-cols-2" data-testid="runner-detail">
      <dl className="grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1.5 rounded-lg border border-border bg-card p-3">
        <dt className="text-muted-foreground">Status</dt>
        <dd>
          <span className="text-foreground">{r.status}{r.paused ? ' · paused' : ''}{r.ready ? '' : ' · not ready'}</span>
          {(r.status_note || r.ready_note) && (
            <span className="block text-foreground-secondary" data-testid="runner-note">
              It says: {[...new Set([r.status_note, r.ready_note].filter(Boolean))].join(' · ')}
            </span>
          )}
          {r.remedy && <span className="mt-1 block text-foreground" data-testid="runner-remedy">To fix: {r.remedy}</span>}
          {r.status === 'stale' && <span className="block text-muted-foreground">No heartbeat lately: the machine is asleep, offline, or its runner stopped.</span>}
        </dd>
        <dt className="text-muted-foreground">Code</dt>
        <dd className="text-foreground-secondary">
          runs <span className="font-mono">{short(r.code_sha)}</span>, server expects <span className="font-mono">{short(r.expected_code_sha)}</span>
          {r.code_current === false && <span className="block text-muted-foreground">It updates itself when it next checks in.</span>}
        </dd>
        <dt className="text-muted-foreground">Its own checks</dt>
        <dd>
          {r.problems.length === 0 ? <span className="text-foreground-secondary">all ok</span> : (
            <ul className="space-y-1">
              {r.problems.map((p) => (
                <li key={p.name}>
                  <span className={clsx('font-mono', p.status === 'fail' ? 'text-destructive' : 'text-warning')}>{p.name}</span>
                  {p.detail && <span className="block text-foreground-secondary">{p.detail}</span>}
                </li>
              ))}
            </ul>
          )}
        </dd>
        <dt className="text-muted-foreground">Agents</dt>
        <dd className="text-foreground-secondary">
          {r.agents.length === 0 ? 'none in this tree' : r.agents.map((a, i) => (
            <span key={a.slug}>{i > 0 && ', '}
              <Link to={`/w/${a.workspace}/agents/${a.slug}/settings#running`} className="text-primary hover:underline">{a.name}</Link>
            </span>
          ))}
        </dd>
        <dt className="text-muted-foreground">Last heartbeat</dt>
        <dd className="text-foreground-secondary">{r.last_heartbeat_at ? relativeTime(r.last_heartbeat_at, now) : 'never'}{r.host && <> · {r.host}</>}</dd>
        <dt className="text-muted-foreground">Usage report</dt>
        <dd className="text-foreground-secondary">
          {r.usage_reported_at ? `last sent ${relativeTime(r.usage_reported_at, now)}` : 'not sent yet — it sends one on current code'}
        </dd>
        <dt className="text-muted-foreground">Lives in</dt>
        <dd className="text-foreground-secondary">{r.workspace ?? '—'}{!r.in_tree && ' (outside this tree)'}</dd>
        <dt className="text-muted-foreground">More</dt>
        <dd className="space-x-3">
          <Link to={`/supervisor?tab=runners&runner=${r.id}`} className="text-primary hover:underline">Open in supervisor</Link>
          <Link to={`/w/${workspace}/settings/topology/runners`} className="text-primary hover:underline">Routing</Link>
        </dd>
      </dl>
      <div className="rounded-lg border border-border bg-card p-3">
        <p className="mb-1 font-semibold text-foreground">Missing sessions · {r.missing_sessions}</p>
        {r.can_administer
          ? <MissingSessions runnerId={r.id} />
          : <p className="text-muted-foreground">Sessions on this box in repos canopy knows, that canopy has no record of. Only the runner's admins see the list{r.owner_email ? ` (${r.owner_email})` : ''}.</p>}
      </div>
    </div>
  )
}
