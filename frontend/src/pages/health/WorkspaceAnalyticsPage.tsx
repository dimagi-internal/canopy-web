import { useState, type JSX, type ReactNode } from 'react'
import { useParams } from 'react-router-dom'

import { getWorkspaceAnalytics, type WorkspaceAnalyticsOut } from '@/api/workspaces'
import { relativeTime } from '@/components/activity/turnLog'
import { DailyBars } from './DailyBars'
import { AdminOnly, HealthViews } from './HealthViews'
import { useTreeRead } from './useTreeRead'

// WORKSPACE ANALYTICS: turns, chats, messages, tokens, cost and people over time,
// across this workspace and (by default) every one below it. Every number comes
// from the server's analytics fact log (`GET /api/workspaces/{slug}/analytics`),
// which retention does not purge — so a number here does not drop when old
// transcripts are deleted.

const RANGES = [7, 30, 90] as const

export function WorkspaceAnalyticsPage(): JSX.Element {
  const { workspace: slug = '' } = useParams()
  const [days, setDays] = useState<number>(30)
  const [tree, setTree] = useState(true)
  const { data, error, forbidden } = useTreeRead(
    () => getWorkspaceAnalytics(slug, { days, tree }), `${slug}:${days}:${tree}`)

  return (
    <div>
      <HealthViews title="Analytics" />
      {!forbidden && <div className="mb-4 flex flex-wrap items-center gap-2 text-[12px]">
        <div className="inline-flex gap-0.5 rounded-lg border border-border bg-card p-0.5" role="group" aria-label="Range">
          {RANGES.map((n) => (
            <button key={n} type="button" onClick={() => setDays(n)} aria-pressed={days === n}
                    className={`min-h-8 rounded-md px-2.5 ${days === n ? 'bg-muted font-medium text-foreground' : 'text-muted-foreground'}`}>
              {n} days
            </button>
          ))}
        </div>
        <label className="inline-flex min-h-8 items-center gap-1.5 text-foreground-secondary">
          <input type="checkbox" checked={tree} onChange={(e) => setTree(e.target.checked)} />
          Include workspaces below
        </label>
      </div>}
      {forbidden ? <AdminOnly what="workspace analytics" />
        : error ? <p className="text-[13px] text-destructive">{error}</p>
          : !data ? <p className="text-[13px] text-muted-foreground">Loading…</p>
            : <Analytics a={data} />}
    </div>
  )
}

const compact = (n: number) => Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(n)
const usd = (n: number) => `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

function Analytics({ a }: { a: WorkspaceAnalyticsOut }): JSX.Element {
  const t = a.totals
  const now = new Date()
  const days = a.series
  return (
    <div className="space-y-6">
      <section aria-label="Totals" className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
        <Stat label="Turns" value={compact(t.turns)} />
        <Stat label="Chats opened" value={compact(t.sessions)} />
        <Stat label="Messages sent" value={compact(t.messages)} />
        <Stat label="Active people" value={compact(t.active_users)} />
        <Stat label="Active agents" value={compact(t.active_agents)} />
        <Stat label="Input tokens" value={compact(t.input_tokens)}
              hint={`${compact(t.cache_read_tokens)} more read from cache`} />
        <Stat label="Output tokens" value={compact(t.output_tokens)}
              hint={t.turns ? `from ${t.turns_with_usage} of ${t.turns} turns` : undefined} />
        <Stat label="Reported cost" value={usd(t.cost_usd)}
              hint={t.turns ? `from ${t.turns_with_cost} of ${t.turns} turns` : undefined} />
      </section>

      <section aria-label="Over time" className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        <DailyBars title="Turns started" days={days} layers={[{ key: 'turns', label: 'Turns', className: 'fill-primary' }]} />
        <DailyBars title="How turns ended" days={days} layers={[
          { key: 'done', label: 'Done', className: 'fill-success' },
          { key: 'failed', label: 'Failed', className: 'fill-destructive' },
          { key: 'lost', label: 'Lost', className: 'fill-warning' },
          { key: 'cancelled', label: 'Cancelled', className: 'fill-muted-foreground' },
        ]} />
        <DailyBars title="Messages sent" days={days} layers={[{ key: 'messages', label: 'Messages', className: 'fill-primary' }]} />
        <DailyBars title="Active people" days={days} rangeTotal={t.active_users} layers={[{ key: 'active_users', label: 'People', className: 'fill-primary' }]} />
        <DailyBars title="Tokens (input + output)" days={days} format={compact} layers={[
          { key: 'input_tokens', label: 'Input', className: 'fill-primary' },
          { key: 'output_tokens', label: 'Output', className: 'fill-muted-foreground' },
        ]} />
        <DailyBars title="Reported cost" days={days} format={usd} layers={[{ key: 'cost_usd', label: 'Cost', className: 'fill-primary' }]} />
      </section>

      <section aria-label="Where turns came from">
        <h2 className="mb-2 text-[13px] font-semibold text-foreground">Where turns came from</h2>
        <div className="flex flex-wrap gap-2 text-[12px]">
          {Object.entries(a.surfaces).sort((x, y) => y[1] - x[1]).map(([s, n]) => (
            <span key={s} className="rounded-md border border-border bg-card px-2 py-1">
              <span className="text-foreground-secondary">{s}</span> <span className="tabular-nums text-foreground">{n}</span>
            </span>
          ))}
          {Object.keys(a.surfaces).length === 0 && <span className="text-muted-foreground">No turns in this range.</span>}
        </div>
      </section>

      <section aria-label="Agents" className="overflow-x-auto">
        <h2 className="mb-2 text-[13px] font-semibold text-foreground">Agents</h2>
        <table className="w-full min-w-[720px] text-[12px] [&_td]:pr-3 [&_th]:pr-3" data-testid="analytics-agents">
          <thead className="text-left text-muted-foreground">
            <tr><th className="py-1 font-medium">Agent</th><NumHead>Turns</NumHead><NumHead>Failed or lost</NumHead><NumHead>Chats</NumHead>
              <NumHead>Messages</NumHead><NumHead>People</NumHead><NumHead>Input</NumHead><NumHead>Output</NumHead><NumHead>Reported cost</NumHead>
              <th className="font-medium">Last activity</th></tr>
          </thead>
          <tbody className="divide-y divide-border">
            {a.agents.map((r) => (
              <tr key={r.agent}>
                <td className="py-1.5 font-mono text-foreground">{r.agent}</td>
                <Num>{r.turns}</Num><Num>{r.failed}</Num><Num>{r.sessions}</Num><Num>{r.messages}</Num>
                <Num>{r.people}</Num><Num>{compact(r.input_tokens)}</Num><Num>{compact(r.output_tokens)}</Num>
                <Num>{usd(r.cost_usd)}</Num>
                <td className="text-foreground-secondary">{r.last_at ? relativeTime(r.last_at, now) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section aria-label="People" className="overflow-x-auto">
        <h2 className="mb-2 text-[13px] font-semibold text-foreground">People</h2>
        <table className="w-full min-w-[820px] text-[12px] [&_td]:pr-3 [&_th]:pr-3" data-testid="analytics-people">
          <thead className="text-left text-muted-foreground">
            <tr><th className="py-1 font-medium">Person</th><NumHead>Active days</NumHead><NumHead>Messages</NumHead><NumHead>Turns</NumHead>
              <NumHead>Chats</NumHead><NumHead>Settings changed</NumHead><NumHead>MCP calls</NumHead><NumHead>Agents</NumHead>
              <th className="font-medium">Uses most</th><NumHead>Tokens</NumHead><NumHead>Reported cost</NumHead>
              <th className="font-medium">Last active</th></tr>
          </thead>
          <tbody className="divide-y divide-border">
            {a.users.map((u) => (
              <tr key={u.user_id}>
                <td className="py-1.5 text-foreground">{u.email}</td>
                <Num>{u.active_days}</Num><Num>{u.messages}</Num><Num>{u.turns}</Num><Num>{u.sessions}</Num>
                <Num>{u.settings}</Num><Num>{u.mcp_calls}</Num><Num>{u.agents}</Num>
                <td className="font-mono text-foreground-secondary">{u.top_agents.join(', ') || '—'}</td>
                <Num>{compact(u.input_tokens + u.output_tokens)}</Num><Num>{usd(u.cost_usd)}</Num>
                <td className="text-foreground-secondary">{u.last_at ? relativeTime(u.last_at, now) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {a.users.length === 0 && <p className="py-3 text-[12px] text-muted-foreground">Nobody did anything here in this range.</p>}
      </section>
    </div>
  )
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }): JSX.Element {
  return (
    <div className="rounded-lg border border-border bg-card px-3 py-2">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="text-lg font-semibold tabular-nums text-foreground">{value}</div>
      {hint && <div className="text-[11px] text-muted-foreground">{hint}</div>}
    </div>
  )
}

function Num({ children }: { children: ReactNode }): JSX.Element {
  return <td className="text-right tabular-nums">{children}</td>
}

function NumHead({ children }: { children: ReactNode }): JSX.Element {
  return <th className="text-right font-medium">{children}</th>
}
