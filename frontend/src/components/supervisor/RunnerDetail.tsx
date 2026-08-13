import { useState, type JSX } from 'react'
import { pauseRunner, unpauseRunner, type RunnerOut } from '@/api/harness'
import type { AgentOut } from '@/api/agents'
import { RunnerAssignments } from '@/components/agents/RunnerAssignments'
import { RunnerDrills } from '@/components/supervisor/RunnerDrills'

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
  onChanged,
}: {
  runner: RunnerOut
  agents: AgentOut[]
  onBack: () => void
  /** A pause changed this runner server-side — hand the fresh row back so the
   *  list behind this view stops disagreeing with the detail in front of it. */
  onChanged?: (runner: RunnerOut) => void
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
  // Every agent row starts EXPANDED — opening a runner's detail should show
  // the assignment editor rows without an extra click (the routing tab is
  // gone; this is now where routing gets edited). A small fleet makes
  // "expand every row" cheap, so simplicity wins over a per-row default.
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(agents.map((a) => a.slug)))
  const toggle = (slug: string) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(slug)) next.delete(slug)
      else next.add(slug)
      return next
    })

  // Pause/resume. `note` is only read on the way IN — resuming clears it
  // server-side, because a reason for a pause that is over is just stale text.
  const [pausing, setPausing] = useState(false)
  const [pauseErr, setPauseErr] = useState<string | null>(null)
  const [note, setNote] = useState('')

  const togglePause = () => {
    setPausing(true)
    setPauseErr(null)
    const call = runner.paused ? unpauseRunner(runner.id) : pauseRunner(runner.id, note.trim())
    call
      .then((fresh) => {
        setNote('')
        onChanged?.(fresh)
      })
      .catch((err: unknown) => {
        // Say it failed. A pause that silently didn't take is the expensive
        // failure here — you walk away believing an account stopped spending.
        setPauseErr(err instanceof Error ? err.message : 'could not change pause state')
      })
      .finally(() => setPausing(false))
  }

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
        {/* What code this box is actually on. Only shown when it says something:
            the cloud runner is a different program and reports neither. */}
        {runner.code_version &&
          row('runner code', `${runner.code_version}${runner.code_sha ? ` (${runner.code_sha.slice(0, 12)})` : ''}`)}
        {runner.code_branch && row('branch', runner.code_branch)}
        {row('status', runner.status ?? 'unknown')}
      </div>

      {/* Pause — the one control this view offers on the runner itself, and the
          only way to park a box from a phone (the alternative is the local
          ~/.canopy/PAUSED sentinel, which needs a shell on that macOS account).
          Pairer-only: POST /pause resolves through _runner_visibility_q, the same
          predicate can_manage reports, so rendering it for anyone else would
          hand out a button that 404s. */}
      {runner.can_manage && (
        <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3" data-testid="runner-pause">
          <div className="flex items-center gap-2">
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
              {runner.paused ? 'Paused' : 'Routing'}
            </span>
            <button
              type="button"
              onClick={togglePause}
              disabled={pausing}
              data-testid="runner-pause-toggle"
              className={`ml-auto rounded-md px-2.5 py-1 text-[12px] font-medium disabled:opacity-50 ${
                runner.paused
                  ? 'bg-primary text-primary-foreground'
                  : 'border border-border text-foreground hover:bg-muted'
              }`}
            >
              {pausing ? '…' : runner.paused ? 'Resume' : 'Pause'}
            </button>
          </div>
          {runner.paused ? (
            <p className="text-[12px] text-muted-foreground" data-testid="runner-pause-why">
              {runner.paused_note || 'No reason given.'} Queued work waits for this
              runner rather than failing — resume to let it claim again.
            </p>
          ) : (
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Why? (e.g. token limit on this account)"
              maxLength={200}
              data-testid="runner-pause-note"
              className="rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground placeholder:text-muted-foreground"
            />
          )}
          {pauseErr && <p className="text-[12px] text-destructive" data-testid="runner-pause-error">{pauseErr}</p>}
        </div>
      )}

      {/* Owner-only surface. The fleet list is workspace-scoped since
          _runner_read_q, so this view can open a runner the caller did not pair;
          drilling POSTs AS that runner and even the drill list is owner-gated, so
          rendering the panel would produce a 404 rather than a control. Say whose
          box it is instead — "nothing here" is indistinguishable from a broken
          page, and naming the owner makes "ask them to declare it" a next step. */}
      {runner.can_manage ? (
        <RunnerDrills runnerId={runner.id} />
      ) : (
        <p className="text-[12px] text-muted-foreground" data-testid="runner-detail-readonly">
          Read-only — this runner was paired by {runner.paired_by_email ?? 'someone else'}, who
          can drill it or change what it declares.
        </p>
      )}

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
