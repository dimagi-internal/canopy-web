import { useState, type JSX } from 'react'
import type { RunnerOut } from '@/api/harness'
import type { AgentOut } from '@/api/agents'
import { RunnerAssignments } from '@/components/agents/RunnerAssignments'

// A runner's full state — the click-through from the Runners tab's runner list.
// Leads with the signals that actually matter: is it AVAILABLE to fire a turn
// (online ∧ ready — a stale runner reporting last-known ready=true is NOT), what
// agents/repos it can drive, and who paired it (the owner that governs what it may
// work for). Below that, the fleet-wide routing matrix — editable in place, so
// "which agents route to me, and at what rank" is answerable without leaving the
// runner detail view. (Assignments are now per-RUNNER, not per-kind, so there is
// no cheap query for "agents that include just this runner" — the matrix's chips
// already surface this runner's name/rank wherever it appears.)
export function RunnerDetail({
  runner,
  agents,
  onBack,
}: {
  runner: RunnerOut
  agents: AgentOut[]
  onBack: () => void
}): JSX.Element {
  const online = runner.status === 'online'
  // Real availability, not last-known ready: a stale runner's ready flag is
  // whatever it reported on its final heartbeat and no longer reflects reality.
  const available = online && runner.ready
  const badge = available
    ? { text: 'available', cls: 'bg-success/15 text-success' }
    : online
      ? { text: 'not ready', cls: 'bg-destructive/15 text-destructive' }
      : { text: runner.status || 'offline', cls: 'bg-muted text-muted-foreground' }
  const caps = (runner.capabilities ?? {}) as { agents?: string[]; projects?: string[] }
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const toggle = (slug: string) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(slug)) next.delete(slug)
      else next.add(slug)
      return next
    })

  const row = (label: string, value: string) => (
    <div className="flex items-baseline justify-between gap-3 border-b border-border py-1.5">
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</span>
      <span className="min-w-0 truncate text-[13px] text-foreground">{value}</span>
    </div>
  )

  const agentRow = (a: AgentOut) => (
    <div key={a.slug} className="rounded-md border border-border bg-background">
      <button
        type="button"
        onClick={() => toggle(a.slug)}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left"
        data-testid={`runner-priority-agent-${a.slug}`}
      >
        <span className="min-w-0 flex-1 truncate text-[13px] text-foreground">{a.name}</span>
        <span className="shrink-0 text-muted-foreground">{expanded.has(a.slug) ? '▾' : '▸'}</span>
      </button>
      {expanded.has(a.slug) && (
        <div className="px-2 pb-2">
          <RunnerAssignments agentSlug={a.slug} />
        </div>
      )}
    </div>
  )

  return (
    <div className="flex flex-col gap-2" data-testid={`runner-detail-${runner.name}`}>
      <button type="button" onClick={onBack} className="self-start text-[12px] text-primary" data-testid="runner-detail-back">
        ← Runners
      </button>
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 rounded-full ${online ? 'bg-success' : 'bg-muted-foreground'}`} />
        <span className="text-[15px] font-semibold text-foreground">{runner.name}</span>
        <span
          data-testid="runner-detail-ready"
          className={`ml-auto rounded px-1.5 py-0.5 text-[11px] ${badge.cls}`}
        >
          {badge.text}
        </span>
      </div>
      {online && !runner.ready && runner.ready_note && (
        <p className="text-[12px] text-destructive" data-testid="runner-detail-why">{runner.ready_note}</p>
      )}
      <div className="rounded-lg border border-border bg-card p-3">
        {row('agents', (caps.agents ?? []).join(', ') || '—')}
        {row('projects', (caps.projects ?? []).join(', ') || '—')}
        {row('kind', runner.kind ?? '')}
        {row('paired by', runner.paired_by_email ?? '—')}
        {/* host only matters for emdash (per-macOS-account session reuse); cloud
            runners report no host, so skip the empty row entirely. */}
        {runner.host && row('host', runner.host)}
        {row('status', runner.status ?? 'unknown')}
      </div>

      {/* The fleet-wide routing matrix, expandable per agent — no cheap query for
          "agents that route to just this runner" now that assignments are
          per-runner rather than per-kind (see file header). */}
      <div className="flex flex-col gap-1.5" data-testid="runner-priority">
        <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Agent routing</span>
        {agents.length === 0 && <p className="text-[12px] text-muted-foreground">No agents.</p>}
        {agents.map((a) => agentRow(a))}
      </div>
    </div>
  )
}
