import { Fragment, useMemo, useState, type JSX, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { clsx } from 'clsx'
import { ChevronDown, ChevronRight } from 'lucide-react'

import { getWorkspaceHealth, type AgentHealthOut, type TopologyRunnerOut, type WorkspaceHealthOut } from '@/api/workspaces'
import { relativeAge } from '@/lib/relativeAge'
import { SetupSteps, StatusIcon } from '@/components/agents/AgentSetupChecklist'
import { AdminOnly, HealthViews } from './HealthViews'
import { RunnersTable } from '@/components/runners/RunnersTable'
import { useTreeRead } from './useTreeRead'
import { scopedPath } from '../../lib/scopedLinks'

// WORKSPACE HEALTH: what is not set up right, across this workspace and every one
// below it. It renders; it never decides. Each agent's steps are its own setup
// checklist and its vault state is THE vault verdict, both computed by the server
// (`GET /api/workspaces/{slug}/health`), so this page and the agent's Settings
// page cannot disagree. Facts and states only — no urgency ranking.

type Filter = 'all' | 'done' | 'used' | 'setup' | 'vault' | AgentHealthOut['vault']['state']

const FILTER_LABEL: Partial<Record<Filter, string>> = {
  all: 'All', setup: 'Steps left', vault: 'Not on canopy-web vault',
}

const VAULT_LABEL: Record<AgentHealthOut['vault']['state'], { text: string; className: string }> = {
  'canopy-web': { text: 'canopy-web', className: 'border-success/30 bg-success/10 text-success' },
  moving: { text: 'Moving', className: 'border-warning/30 bg-warning/10 text-warning' },
  '1password': { text: '1Password', className: 'border-border bg-muted text-foreground-secondary' },
  none: { text: 'No secrets', className: 'border-border bg-card text-muted-foreground' },
}

export function WorkspaceHealthPage(): JSX.Element {
  const { workspace: slug = '' } = useParams()
  const { data, error, forbidden } = useTreeRead(() => getWorkspaceHealth(slug), slug)
  return (
    <div>
      <HealthViews title="Health" />
      {forbidden ? <AdminOnly what="workspace health" />
        : error ? <p className="text-[13px] text-destructive">{error}</p>
          : !data ? <p className="text-[13px] text-muted-foreground">Loading…</p>
            : <Health health={data} />}
    </div>
  )
}

function Health({ health }: { health: WorkspaceHealthOut }): JSX.Element {
  const [filter, setFilter] = useState<Filter>('all')
  const [q, setQ] = useState('')
  const [open, setOpen] = useState<string | null>(null)
  const t = health.totals
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return health.agents.filter((a) =>
      matches(a, filter)
      && (!needle || [a.name, a.slug, a.owner_email, a.workspace].some((s) => s.toLowerCase().includes(needle))))
  }, [health.agents, filter, q])
  const now = new Date()
  // A tile filters the agents table to what it counts and brings it into view.
  const show = (f: Filter) => () => {
    setFilter(f)
    document.getElementById('health-agents')?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
  }

  return (
    <div className="space-y-6">
      <section aria-label="Summary" className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-9">
        <Tile label="Agents" value={t.agents} onClick={show('all')} />
        <Tile label="Used, 30 days" value={t.used_30d} hint="a turn, chat or message" onClick={show('used')} />
        <Tile label="Fully set up" value={t.set_up} onClick={show('done')} />
        <Tile label="With steps left" value={t.needs_setup} onClick={show('setup')} />
        <Tile label="On canopy-web vault" value={t.vault_canopy_web} onClick={show('canopy-web')} />
        <Tile label="Moving" value={t.vault_moving} hint="some secrets elsewhere" onClick={show('moving')} />
        <Tile label="On 1Password" value={t.vault_1password} onClick={show('1password')} />
        <Tile label="No secrets" value={t.vault_none} onClick={show('none')} />
        <Tile label="Runners online" value={t.runners_online} hint={`of ${t.runners}`}
              onClick={() => document.getElementById('health-runners')?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })} />
      </section>

      <section aria-label="Workspaces">
        <h2 className="mb-2 text-[13px] font-semibold text-foreground">Workspaces</h2>
        <table className="w-full text-[12px] [&_td]:pr-3 [&_th]:pr-3" data-testid="health-workspaces">
          <thead className="text-left text-muted-foreground">
            <tr><th className="py-1 font-medium">Workspace</th><th className="font-medium">Agents</th>
              <th className="font-medium">Shared 1Password vault</th></tr>
          </thead>
          <tbody className="divide-y divide-border">
            {health.workspaces.map((w) => (
              <tr key={w.slug}>
                <td className="py-1.5" style={{ paddingLeft: `${w.depth * 16}px` }}>
                  <Link to={scopedPath(w.slug, '/health')} className="text-foreground hover:underline">{w.name}</Link>
                  <span className="ml-1 font-mono text-muted-foreground">{w.slug}</span>
                </td>
                <td className="tabular-nums"><CountLink n={w.agents} to={scopedPath(w.slug, '/agents')} label="agents" /></td>
                <td className="text-foreground-secondary">
                  <Link to={scopedPath(w.slug, '/settings/secrets')} className="hover:underline">
                  {w.shared_vault.vault
                    ? <>{w.shared_vault.vault}{w.shared_vault.key_set ? '' : ' · no key stored'}
                      {w.shared_vault.inherited_from && <span className="text-muted-foreground"> · from {w.shared_vault.inherited_from}</span>}</>
                    : <span className="text-muted-foreground">none</span>}
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section aria-label="Agents" id="health-agents" className="scroll-mt-4">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <h2 className="mr-2 text-[13px] font-semibold text-foreground">Agents</h2>
          {(['all', 'setup', 'vault'] as const).map((f) => (
            <button key={f} type="button" onClick={() => setFilter(f)}
                    className={clsx('min-h-8 rounded-md border px-2.5 text-[12px]',
                      filter === f ? 'border-foreground/30 bg-muted text-foreground' : 'border-border text-muted-foreground hover:text-foreground')}>
              {FILTER_LABEL[f]}
            </button>
          ))}
          {!(filter in FILTER_LABEL) && (
            <button type="button" onClick={() => setFilter('all')} data-testid="tile-filter"
                    className="min-h-8 rounded-md border border-foreground/30 bg-muted px-2.5 text-[12px] text-foreground">
              {filter === 'done' ? 'Fully set up' : filter === 'used' ? 'Used, 30 days' : `Vault: ${VAULT_LABEL[filter as AgentHealthOut['vault']['state']].text}`} ×
            </button>
          )}
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter by name, owner, workspace"
                 aria-label="Filter agents"
                 className="ml-auto min-h-8 w-full rounded-md border border-border bg-card px-2.5 text-[12px] sm:w-64" />
        </div>
        <p className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          {health.agents[0] && <span>Setup, in order: {health.agents[0].steps.map((st) => st.title).join(' · ')}.</span>}
          {(['done', 'todo', 'waiting', 'optional'] as const).map((st) => (
            <span key={st} className="inline-flex items-center gap-1"><StatusIcon status={st} />{st === 'todo' ? 'to do' : st}</span>
          ))}
          <span>Open a row for each step and its fix.</span>
        </p>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1380px] text-[12px] [&_td]:pr-3 [&_th]:pr-3" data-testid="health-agents">
            <thead className="text-left text-muted-foreground">
              <tr>
                <th className="py-1 font-medium">Agent</th><th className="font-medium">Setup</th>
                <th className="font-medium">Vault</th><th className="font-medium">Owner</th>
                <th className="font-medium">Workspace</th><th className="font-medium">Last turn</th>
                <th className="font-medium text-right" title="Turns started in 30 days, and how many of those that finished failed or were lost">Turns, 30 days</th>
                <th className="font-medium text-right" title="People who asked it, chatted or messaged in 30 days">People</th>
                <th className="font-medium" title="The runners its finished turns ran on in 30 days, and how many each">Ran on</th>
                <th className="font-medium" title="Open sessions, and when any of its sessions last did something">Sessions</th>
                <th className="font-medium text-right">Projects</th>
                <th className="font-medium text-right" title="Suggested or in progress">Open tasks</th>
                <th className="font-medium text-right" title="Open tasks with an unanswered ask, or parked on a person">Waiting on people</th>
                <th className="font-medium text-right">Skills</th><th className="font-medium text-right">Schedules</th>
                <th className="font-medium" title="What people other than its admins may ask it to do (its published interface)">Who can use it</th>
                <th className="font-medium" title="manual: its turns stop for a person's approval before acting or sending. auto: they run through.">Turn mode</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.map((a) => (
                <Fragment key={a.slug}>
                  <tr className="cursor-pointer hover:bg-muted/50" data-agent={a.slug}
                      onClick={() => setOpen(open === a.slug ? null : a.slug)}>
                    <td className="py-1.5">
                      <button type="button" aria-expanded={open === a.slug}
                              className="inline-flex items-center gap-1 text-left font-medium text-foreground">
                        {open === a.slug
                          ? <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
                          : <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />}
                        {a.name}
                      </button>
                      <span className="ml-1 font-mono text-muted-foreground">{a.slug}</span>
                    </td>
                    <td>
                      <span className="inline-flex items-center gap-0.5" aria-label={`${a.left} steps left`}>
                        {a.steps.map((s) => <span key={s.key} title={`${s.title}: ${s.status}`}><StatusIcon status={s.status} /></span>)}
                        <span className="ml-1 whitespace-nowrap tabular-nums text-muted-foreground">{a.left ? `${a.left} left` : 'done'}</span>
                      </span>
                    </td>
                    <td><CellLink to={scopedPath(a.workspace, `/agents/${a.slug}/settings#credentials`)}><VaultBadge agent={a} /></CellLink></td>
                    <td className="text-foreground-secondary">
                      {a.owner_email ? <a href={`mailto:${a.owner_email}`} onClick={stop} className="hover:underline">{a.owner_email}</a> : '—'}
                    </td>
                    <td className="font-mono text-foreground-secondary"><CellLink to={scopedPath(a.workspace, '/health')}>{a.workspace}</CellLink></td>
                    <td className="text-foreground-secondary">
                      {a.last_turn_at ? <CellLink to={scopedPath(a.workspace, `/agents/${a.slug}/turns`)}>{relativeAge(a.last_turn_at, now, { seconds: false })}</CellLink> : 'never'}
                    </td>
                    <td className="whitespace-nowrap text-right tabular-nums">
                      <CountLink n={a.turns_30d} to={scopedPath(a.workspace, `/agents/${a.slug}/turns`)} label="turns" />
                      {a.failed_30d > 0 && <span className="text-destructive"> · {a.failed_30d} failed</span>}
                    </td>
                    <td className="text-right tabular-nums text-foreground-secondary">{a.people_30d || '—'}</td>
                    <td className="whitespace-nowrap text-foreground-secondary" data-testid="ran-on">
                      {a.ran_on.length === 0 ? '—' : a.ran_on.map((r, i) => (
                        <span key={r.name} title={`${r.kind || 'runner'} · ${r.turns} turn${r.turns === 1 ? '' : 's'}`}>
                          {i > 0 && ' · '}{r.name} <span className="tabular-nums text-muted-foreground">{r.turns}</span>
                        </span>
                      ))}
                    </td>
                    <td className="whitespace-nowrap text-foreground-secondary" title="Open the row for its latest sessions">
                      {a.active_sessions ? <span className="text-foreground">{a.active_sessions} open</span> : '0 open'}
                      {a.last_session_at && <span className="text-muted-foreground"> · {relativeAge(a.last_session_at, now, { seconds: false })}</span>}
                    </td>
                    <td className="text-right tabular-nums">
                      <CountLink n={a.projects} to={scopedPath(a.workspace, `/agents/${a.slug}/projects`)} label="projects" />
                    </td>
                    <td className="text-right tabular-nums">
                      <CountLink n={a.open_tasks} to={scopedPath(a.workspace, `/agents/${a.slug}/tasks`)} label="open tasks" />
                    </td>
                    <td className="text-right tabular-nums">
                      <CountLink n={a.waiting_tasks} to={scopedPath(a.workspace, `/agents/${a.slug}/tasks`)} label="tasks waiting on people" />
                    </td>
                    <td className="text-right tabular-nums"><CountLink n={a.skills} to={scopedPath(a.workspace, `/agents/${a.slug}/skills`)} label="skills" /></td>
                    <td className="text-right tabular-nums"><CountLink n={a.schedules} to={scopedPath(a.workspace, `/agents/${a.slug}/schedules`)} label="schedules" /></td>
                    <td className="whitespace-nowrap text-foreground-secondary"
                        title={a.full_access.length ? `Whole agent for: ${a.full_access.join(', ')}` : undefined}>
                      <CellLink to={scopedPath(a.workspace, `/agents/${a.slug}/settings#reach`)}>{whoCanUse(a)}</CellLink>
                    </td>
                    <td className="whitespace-nowrap text-foreground-secondary">
                      <CellLink to={scopedPath(a.workspace, `/agents/${a.slug}/settings#running`)}>
                        {a.turn_mode === 'manual' ? 'Manual · asks first' : a.turn_mode === 'auto' ? 'Auto' : a.turn_mode}
                      </CellLink>
                    </td>
                  </tr>
                  {open === a.slug && (
                    <tr><td colSpan={17} className="bg-muted/30 p-3"><AgentDetail agent={a} /></td></tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
          {rows.length === 0 && <p className="py-3 text-[12px] text-muted-foreground">No agents match.</p>}
        </div>
      </section>

      <Runners runners={health.runners} workspace={health.root} />
    </div>
  )
}

/** "Who can use it" in words: its admins, plus whatever its interface publishes —
 *  `full:` rules (the whole agent) and capabilities (confined to one job each). */
export function whoCanUse(a: Pick<AgentHealthOut, 'has_interface' | 'capabilities' | 'full_access'>): string {
  if (!a.has_interface) return 'Admins only'
  const parts = []
  if (a.full_access.length) parts.push(`full: ${a.full_access.length} rule${a.full_access.length === 1 ? '' : 's'}`)
  if (a.capabilities) parts.push(`${a.capabilities} capabilit${a.capabilities === 1 ? 'y' : 'ies'}`)
  return `Admins + ${parts.join(' · ')}`
}

function matches(a: AgentHealthOut, f: Filter): boolean {
  if (f === 'all') return true
  if (f === 'done') return a.left === 0
  if (f === 'used') return a.turns_30d > 0 || a.people_30d > 0
  if (f === 'setup') return a.left > 0
  if (f === 'vault') return a.vault.state === 'moving' || a.vault.state === '1password'
  return a.vault.state === f
}

type RunnerRow = TopologyRunnerOut

/** The runners section: THE runner table (shared with Topology → Runners). */
function Runners({ runners, workspace }: { runners: readonly RunnerRow[]; workspace: string }): JSX.Element {
  return (
    <section aria-label="Runners" id="health-runners" className="scroll-mt-4">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h2 className="text-[13px] font-semibold text-foreground">Runners</h2>
        <Link to={scopedPath(workspace, '/settings/topology/runners')} className="text-[12px] text-primary hover:underline">
          Routing and drills
        </Link>
      </div>
      <RunnersTable runners={runners} workspace={workspace} />
    </section>
  )
}

// A link inside a row that opens on click must not also toggle the row.
const stop = (e: { stopPropagation: () => void }) => e.stopPropagation()

/** A count that opens the page it counts; a zero is just a zero. */
function CountLink({ n, to, label }: { n: number; to: string; label: string }): JSX.Element {
  if (!n) return <span className="text-muted-foreground">0</span>
  return (
    <Link to={to} onClick={stop} aria-label={`${n} ${label}`}
          className="text-primary hover:underline">{n}</Link>
  )
}

/** A cell's value that opens where it is set. */
function CellLink({ to, children }: { to: string; children: ReactNode }): JSX.Element {
  return <Link to={to} onClick={stop} className="hover:underline">{children}</Link>
}

function Tile({ label, value, hint, onClick }: { label: string; value: number; hint?: string; onClick: () => void }): JSX.Element {
  return (
    <button type="button" onClick={onClick}
            className="rounded-lg border border-border bg-card px-3 py-2 text-left hover:border-foreground/30">
      <div className="text-[11px] text-muted-foreground">{label}{hint && <span> · {hint}</span>}</div>
      <div className="text-lg font-semibold tabular-nums text-foreground">{value}</div>
    </button>
  )
}

function VaultBadge({ agent }: { agent: AgentHealthOut }): JSX.Element {
  const v = agent.vault
  const label = VAULT_LABEL[v.state]
  const extra = v.in_1password.length + v.missing.length
  return (
    <span className="inline-flex items-center gap-1">
      <span className={clsx('rounded border px-1.5 py-0.5 text-[11px]', label.className)}>{label.text}</span>
      {v.state === 'moving' && <span className="text-muted-foreground">{extra} to move</span>}
      {v.machines_waiting.length > 0 && <span className="text-muted-foreground">{v.machines_waiting.length} machine waiting</span>}
    </span>
  )
}

/** Mail the owner what is left, with the link to the agent's settings. */
export function ownerMailto(agent: AgentHealthOut, origin: string): string {
  const left = agent.steps.filter((s) => s.status === 'todo' || s.status === 'waiting')
  const lines = left.map((s) => `- ${s.title}: ${s.detail}${s.href ? `\n  ${s.href.startsWith('http') ? s.href : origin + s.href}` : ''}`)
  const body = [`${agent.name} has ${left.length} setup step${left.length === 1 ? '' : 's'} left:`, '', ...lines, '',
    `Its settings: ${origin}/w/${agent.workspace}/agents/${agent.slug}/settings`].join('\n')
  return `mailto:${encodeURIComponent(agent.owner_email)}?subject=${encodeURIComponent(`Finish setting up ${agent.name}`)}&body=${encodeURIComponent(body)}`
}

function AgentDetail({ agent }: { agent: AgentHealthOut }): JSX.Element {
  const v = agent.vault
  return (
    <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
      <div className="rounded-lg border border-border bg-card">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2 text-[12px]">
          <span className="text-muted-foreground">
            <span className="font-semibold text-foreground">Setup</span> — the same steps as its{' '}
            <Link to={scopedPath(agent.workspace, `/agents/${agent.slug}/settings`)} className="text-primary hover:underline">Settings page</Link>.
          </span>
          {!agent.can_fix && agent.left > 0 && (
            agent.owner_email
              ? <a href={ownerMailto(agent, window.location.origin)} className="text-primary hover:underline" data-testid="send-to-owner">
                  Only its owner or an admin can fix this — email {agent.owner_email}
                </a>
              : <span className="text-muted-foreground">It has no owner; a workspace owner can take it over.</span>
          )}
        </div>
        <SetupSteps steps={agent.steps} canFix={agent.can_fix} />
      </div>
      <div className="rounded-lg border border-border bg-card p-3 text-[12px]">
        <p className="mb-2 font-semibold text-foreground">Secrets · reads from {v.source}</p>
        {v.refs.length === 0 ? <p className="text-muted-foreground">Declares no secrets.</p> : (
          <table className="w-full">
            <tbody className="divide-y divide-border">
              {v.refs.map((r) => (
                <tr key={r.name}>
                  <td className="py-1 font-mono">{r.name}</td>
                  <td className={clsx('text-right', r.status === 'missing' ? 'text-destructive'
                    : r.status === '1password' && v.source === 'canopy-web' ? 'text-warning' : 'text-foreground-secondary')}>
                    {REF_LABEL[r.status]}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {v.op_vault && <p className="mt-2 text-muted-foreground">1Password vault {v.op_vault}{v.op_key_set ? ', key stored' : ', no key stored'}</p>}
        {v.machines_waiting.length > 0 && (
          <p className="mt-2 text-muted-foreground">Waiting to be let in: {v.machines_waiting.join(', ')}</p>
        )}
        <RecentSessions agent={agent} />
      </div>
    </div>
  )
}

/** Its latest sessions — only those the viewer may open (the server filters). */
function RecentSessions({ agent }: { agent: AgentHealthOut }): JSX.Element {
  const now = new Date()
  return (
    <div className="mt-4 border-t border-border pt-3" data-testid="recent-sessions">
      <p className="mb-1 font-semibold text-foreground">Latest sessions</p>
      {agent.recent_sessions.length === 0
        ? <p className="text-muted-foreground">{agent.last_session_at ? 'None you can open.' : 'None yet.'}</p>
        : (
          <ul className="space-y-1">
            {agent.recent_sessions.map((s) => (
              <li key={s.id} className="flex items-baseline justify-between gap-2">
                <Link to={scopedPath(s.workspace, `/chat/${s.id}`)} className="min-w-0 truncate text-primary hover:underline">
                  {s.title || 'Untitled session'}
                </Link>
                <span className="shrink-0 text-muted-foreground">
                  {s.active && <span className="mr-1 text-success">open ·</span>}{relativeAge(s.at, now, { seconds: false })}
                </span>
              </li>
            ))}
          </ul>
        )}
    </div>
  )
}

const REF_LABEL: Record<AgentHealthOut['vault']['refs'][number]['status'], string> = {
  stored: 'in canopy-web',
  literal: 'plain value in runtime.yaml',
  local: 'minted per machine',
  '1password': 'in 1Password',
  missing: 'not stored anywhere',
}
