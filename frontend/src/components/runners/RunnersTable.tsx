import { Fragment, useState, type JSX } from 'react'
import { Link } from 'react-router-dom'
import { clsx } from 'clsx'
import { ArrowRight, ChevronDown, ChevronRight } from 'lucide-react'

import type { TopologyRunnerOut } from '@/api/workspaces'
import { relativeAge } from '@/lib/relativeAge'
import { statusTone } from '@/pages/runnerTopology'
import { fixHref } from '@/components/supervisor/RunnerHealth'
import { runnerHref, scopedPath } from '../../lib/scopedLinks'

// THE RUNNER TABLE — one table for Health and Topology → Runners, over the
// topology's own runner rows (`harness.topology.build`). Facts and states only:
// what each box reports, never a verdict. Every row opens its detail: each
// column spelled out, with where to change it. The detail is read-only; the
// runner itself is changed in one place, the supervisor's runner page, which
// the detail leads with.

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
  // A box running several isolation units lists them together (the server
  // orders them so); each row says which machine and which unit it is.
  const perMachine = new Map<string, number>()
  for (const r of runners) if (r.machine) perMachine.set(r.machine, (perMachine.get(r.machine) ?? 0) + 1)
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[960px] text-[12px] [&_td]:pr-3 [&_th]:pr-3" data-testid="runners-table">
        <thead className="text-left text-muted-foreground">
          <tr><th className="py-1 font-medium">Runner</th><th className="font-medium">Owner</th>
            <th className="font-medium">Status</th><th className="font-medium" title="Running the code the deployed server expects">Code</th>
            <th className="font-medium">Its own checks</th><th className="font-medium text-right">Agents</th>
            <th className="font-medium" title="When the box last sent the token counts of its sessions">Usage report</th>
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
                  {r.unit && <span className="ml-1 text-muted-foreground" data-testid="runner-unit">· unit {r.unit}</span>}
                  {r.machine && (perMachine.get(r.machine) ?? 0) > 1 && (
                    <span className="ml-1 text-muted-foreground" data-testid="runner-machine">· on {r.machine}</span>
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
                <td className="whitespace-nowrap text-foreground-secondary" data-testid="runner-checks"
                    title={r.problems.map((p) => p.name).join(', ') || undefined}>
                  <CheckCounts problems={r.problems} />
                </td>
                <td className="text-right tabular-nums">{r.agent_count}</td>
                <td className="whitespace-nowrap text-foreground-secondary">
                  {r.usage_reported_at ? relativeAge(r.usage_reported_at, now, { seconds: false }) : 'no report'}
                </td>
                <td className="whitespace-nowrap text-foreground-secondary">{r.last_heartbeat_at ? relativeAge(r.last_heartbeat_at, now, { seconds: false }) : 'never'}</td>
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

/** A row's checks as counts — the names, one per line with their fix, are the
 *  detail's; a dozen names in a cell made the row wrap and the table overflow. */
function CheckCounts({ problems }: { problems: TopologyRunnerOut['problems'] }): JSX.Element {
  const fail = problems.filter((p) => p.status === 'fail').length
  const warn = problems.length - fail
  if (problems.length === 0) return <>all ok</>
  return (
    <>
      {fail > 0 && <span className="text-destructive">{fail} failing</span>}
      {fail > 0 && warn > 0 && ' · '}
      {warn > 0 && <span className="text-warning">{warn} warning{warn === 1 ? '' : 's'}</span>}
    </>
  )
}

/** One runner, each column spelled out: its status in its own words and the fix
 *  for a known cause, the code it runs against what the server expects, every
 *  check that is not ok with its detail, the agents it serves, and its usage
 *  report. */
function RunnerDetail({ runner: r, workspace }: { runner: TopologyRunnerOut; workspace: string }): JSX.Element {
  const now = new Date()
  const short = (sha: string) => sha ? sha.slice(0, 8) : 'unknown'
  const manage = runnerHref({ id: r.id, workspace: r.workspace ?? workspace })
  // A `github.<agent>` check is fixed on that agent; every other check (its
  // Claude sign-in, a refresh) on the runner itself.
  const agentWs = (slug: string) => r.agents.find((a) => a.slug === slug)?.workspace
  const fixFor = (name: string) => fixHref(name, agentWs) ?? (/^github\./.test(name) ? null : manage)
  return (
    <div className="space-y-3 text-[12px]" data-testid="runner-detail">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-border bg-card p-3">
        <div className="min-w-0 flex-1">
          <p className="font-semibold text-foreground">{r.name}</p>
          <p className="text-muted-foreground">
            A read-only summary. {r.can_administer
              ? 'Sign it in, pause it, set its flags, workspaces and admins on its runner page.'
              : `Its runner page shows everything; changing it is for its owner${r.owner_email ? ` (${r.owner_email})` : ''} and admins.`}
          </p>
        </div>
        <Link to={scopedPath(workspace, '/settings/topology/runners')} className="text-primary hover:underline">Routing</Link>
        <Link to={manage} data-testid="runner-manage"
              className="inline-flex min-h-8 items-center gap-1 rounded-md bg-primary px-3 font-medium text-primary-foreground hover:bg-primary/90">
          {r.can_administer ? 'Manage runner' : 'Open runner'} <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
        </Link>
      </div>
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
          {r.code_current === false && <span className="block text-muted-foreground">It updates itself within about 30 minutes, after any turn in flight; still behind after that, it raises an alert.</span>}
        </dd>
        <dt className="text-muted-foreground">Its own checks</dt>
        <dd>
          {r.problems.length === 0 ? <span className="text-foreground-secondary">all ok</span> : (
            <ul className="space-y-1">
              {r.problems.map((p) => {
                const fix = fixFor(p.name)
                return (
                  <li key={p.name}>
                    <span className={clsx('font-mono', p.status === 'fail' ? 'text-destructive' : 'text-warning')}>{p.name}</span>
                    {fix && <Link to={fix} className="ml-2 text-primary hover:underline">Fix →</Link>}
                    {p.detail && <span className="block text-foreground-secondary">{p.detail}</span>}
                  </li>
                )
              })}
            </ul>
          )}
        </dd>
        <dt className="text-muted-foreground">Agents</dt>
        <dd className="text-foreground-secondary">
          {r.agents.length === 0 ? 'none in this tree' : r.agents.map((a, i) => (
            <span key={a.slug}>{i > 0 && ', '}
              <Link to={scopedPath(a.workspace, `/agents/${a.slug}/settings#running`)} className="text-primary hover:underline">{a.name}</Link>
            </span>
          ))}
        </dd>
        <dt className="text-muted-foreground">Last heartbeat</dt>
        <dd className="text-foreground-secondary">{r.last_heartbeat_at ? relativeAge(r.last_heartbeat_at, now, { seconds: false }) : 'never'}{r.host && <> · {r.host}</>}</dd>
        <dt className="text-muted-foreground">Usage report</dt>
        <dd className="text-foreground-secondary">
          {r.usage_reported_at ? `last sent ${relativeAge(r.usage_reported_at, now, { seconds: false })}` : 'not sent yet — it sends one on current code'}
        </dd>
        <dt className="text-muted-foreground">Lives in</dt>
        <dd className="text-foreground-secondary">{r.workspace ?? '—'}{!r.in_tree && ' (outside this tree)'}</dd>
      </dl>
    </div>
  )
}
