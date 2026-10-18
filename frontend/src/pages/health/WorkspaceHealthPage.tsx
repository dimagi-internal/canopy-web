import { Fragment, useMemo, useState, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'
import { clsx } from 'clsx'
import { ChevronDown, ChevronRight } from 'lucide-react'

import { getWorkspaceHealth, type AgentHealthOut, type WorkspaceHealthOut } from '@/api/workspaces'
import { relativeTime } from '@/components/activity/turnLog'
import { SetupSteps, StatusIcon } from '@/components/agents/AgentSetupChecklist'
import { AdminOnly, HealthViews } from './HealthViews'
import { useTreeRead } from './useTreeRead'

// WORKSPACE HEALTH: what is not set up right, across this workspace and every one
// below it. It renders; it never decides. Each agent's steps are its own setup
// checklist and its vault state is THE vault verdict, both computed by the server
// (`GET /api/workspaces/{slug}/health`), so this page and the agent's Settings
// page cannot disagree. Facts and states only — no urgency ranking.

type Filter = 'all' | 'setup' | 'vault'

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
      (filter === 'all' || (filter === 'setup' ? a.left > 0 : a.vault.state === 'moving' || a.vault.state === '1password'))
      && (!needle || [a.name, a.slug, a.owner_email, a.workspace].some((s) => s.toLowerCase().includes(needle))))
  }, [health.agents, filter, q])
  const now = new Date()

  return (
    <div className="space-y-6">
      <section aria-label="Summary" className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
        <Tile label="Agents" value={t.agents} />
        <Tile label="Fully set up" value={t.set_up} />
        <Tile label="With steps left" value={t.needs_setup} />
        <Tile label="On canopy-web vault" value={t.vault_canopy_web} />
        <Tile label="Moving" value={t.vault_moving} hint="some secrets elsewhere" />
        <Tile label="On 1Password" value={t.vault_1password} />
        <Tile label="No secrets" value={t.vault_none} />
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
                  <Link to={`/w/${w.slug}/settings/secrets`} className="text-foreground hover:underline">{w.name}</Link>
                  <span className="ml-1 font-mono text-muted-foreground">{w.slug}</span>
                </td>
                <td className="tabular-nums">{w.agents}</td>
                <td className="text-foreground-secondary">
                  {w.shared_vault.vault
                    ? <>{w.shared_vault.vault}{w.shared_vault.key_set ? '' : ' · no key stored'}
                      {w.shared_vault.inherited_from && <span className="text-muted-foreground"> · from {w.shared_vault.inherited_from}</span>}</>
                    : <span className="text-muted-foreground">none</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section aria-label="Agents">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <h2 className="mr-2 text-[13px] font-semibold text-foreground">Agents</h2>
          {(['all', 'setup', 'vault'] as const).map((f) => (
            <button key={f} type="button" onClick={() => setFilter(f)}
                    className={clsx('min-h-8 rounded-md border px-2.5 text-[12px]',
                      filter === f ? 'border-foreground/30 bg-muted text-foreground' : 'border-border text-muted-foreground hover:text-foreground')}>
              {f === 'all' ? 'All' : f === 'setup' ? 'Steps left' : 'Not on canopy-web vault'}
            </button>
          ))}
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
          <table className="w-full min-w-[880px] text-[12px] [&_td]:pr-3 [&_th]:pr-3" data-testid="health-agents">
            <thead className="text-left text-muted-foreground">
              <tr>
                <th className="py-1 font-medium">Agent</th><th className="font-medium">Setup</th>
                <th className="font-medium">Vault</th><th className="font-medium">Owner</th>
                <th className="font-medium">Workspace</th><th className="font-medium">Last turn</th>
                <th className="font-medium text-right">Turns, 30 days</th><th className="font-medium text-right">Skills</th>
                <th className="font-medium text-right">Schedules</th><th className="font-medium">Interface</th>
                <th className="font-medium">Mode</th>
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
                    <td><VaultBadge agent={a} /></td>
                    <td className="text-foreground-secondary">{a.owner_email || '—'}</td>
                    <td className="font-mono text-foreground-secondary">{a.workspace}</td>
                    <td className="text-foreground-secondary">{a.last_turn_at ? relativeTime(a.last_turn_at, now) : 'never'}</td>
                    <td className="text-right tabular-nums">{a.turns_30d}</td>
                    <td className="text-right tabular-nums">{a.skills}</td>
                    <td className="text-right tabular-nums">{a.schedules}</td>
                    <td className="text-foreground-secondary">{a.has_interface ? 'published' : 'none'}</td>
                    <td className="text-foreground-secondary">{a.turn_mode}</td>
                  </tr>
                  {open === a.slug && (
                    <tr><td colSpan={11} className="bg-muted/30 p-3"><AgentDetail agent={a} /></td></tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
          {rows.length === 0 && <p className="py-3 text-[12px] text-muted-foreground">No agents match.</p>}
        </div>
      </section>
    </div>
  )
}

function Tile({ label, value, hint }: { label: string; value: number; hint?: string }): JSX.Element {
  return (
    <div className="rounded-lg border border-border bg-card px-3 py-2">
      <div className="text-[11px] text-muted-foreground">{label}{hint && <span> · {hint}</span>}</div>
      <div className="text-lg font-semibold tabular-nums text-foreground">{value}</div>
    </div>
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
            <Link to={`/w/${agent.workspace}/agents/${agent.slug}/settings`} className="text-primary hover:underline">Settings page</Link>.
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
      </div>
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
