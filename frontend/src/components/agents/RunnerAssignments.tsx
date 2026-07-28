import { useEffect, useMemo, useRef, useState, type JSX } from 'react'
import { getAgentRunners, putAgentRunners, type AgentRunnerOut } from '@/api/agents'
import { listRunners, type RunnerOut } from '@/api/harness'
import { RunnerSourceRules } from '@/components/agents/RunnerSourceRules'

// The routing-matrix row: which RUNNERS (not kinds) this agent will route to, in
// rank order. Supersedes RunnerOrder's kind-based `runner_preference` — a rank is
// now a specific paired runner, not a class of runner. Every mutation (reorder,
// toggle, add) computes the full ordered rows list and PUTs it optimistically
// (local state first, revert on failure) so the row never blocks on a round-trip.
//
// There is deliberately NO removal affordance. What used to be "×" (drop the
// runner from the list) is now a toggle: clicking it flips `enabled` and PUTs
// the full rows list. A disabled row stays in the list — rank preserved,
// rendered greyed — it just never routes (apps/harness/services.py's claim
// path excludes it entirely). Re-adding an already-listed-but-disabled runner
// from the + menu just re-enables it in place.

const KIND_GLYPH: Record<string, string> = {
  emdash: 'emdash',
  cloud: 'cloud',
}

function dotClass(r: AgentRunnerOut): string {
  if (!r.online) return 'bg-destructive'
  return r.ready ? 'bg-success' : 'bg-warning'
}

function dotTitle(r: AgentRunnerOut): string {
  if (!r.online) return 'offline'
  return r.ready ? 'online — ready' : 'online — not ready'
}

// The rows form PUT sends: {runnerId, enabled}, index = rank. Pure
// row-list transforms, extracted so the logic is unit-testable without a
// React renderer (see RunnerAssignments.test.tsx).
export type RunnerRow = { runnerId: string; enabled: boolean }

function toRows(rows: readonly AgentRunnerOut[]): RunnerRow[] {
  return rows.map((r) => ({ runnerId: r.runner_id, enabled: r.enabled }))
}

export function nextRowsForMove(rows: readonly AgentRunnerOut[], i: number, d: -1 | 1): RunnerRow[] | null {
  const j = i + d
  if (j < 0 || j >= rows.length) return null
  const next = toRows(rows)
  ;[next[i], next[j]] = [next[j], next[i]]
  return next
}

// Replaces the old remove — flips `enabled` in place, rank untouched. This is
// the whole of what used to be the "×" remove button: no row is ever dropped
// from the list by user action anymore.
export function nextRowsForToggle(rows: readonly AgentRunnerOut[], id: string): RunnerRow[] {
  return toRows(rows).map((r) => (r.runnerId === id ? { ...r, enabled: !r.enabled } : r))
}

// Adding an id already in the list (a disabled row) re-enables it in place
// rather than duplicating it; a genuinely new id is appended enabled.
export function nextRowsForAdd(rows: readonly AgentRunnerOut[], id: string): RunnerRow[] {
  const existing = toRows(rows)
  if (existing.some((r) => r.runnerId === id)) {
    return existing.map((r) => (r.runnerId === id ? { ...r, enabled: true } : r))
  }
  return [...existing, { runnerId: id, enabled: true }]
}

// Build the optimistic row list for a next-rows ordering: rows we already
// know about are re-ranked/re-enabled in place; an id not yet in `prev` (a
// fresh add) is patched in from the fleet list so the chip renders
// immediately instead of flashing empty until the PUT returns.
export function buildOptimisticRows(
  prev: readonly AgentRunnerOut[],
  nextRows: readonly RunnerRow[],
  fleet: readonly RunnerOut[],
): AgentRunnerOut[] {
  const byId = new Map(prev.map((r) => [r.runner_id, r]))
  return nextRows.map((row, i) => {
    const existing = byId.get(row.runnerId)
    if (existing) return { ...existing, rank: i + 1, enabled: row.enabled }
    const f = fleet.find((r) => r.id === row.runnerId)
    return {
      runner_id: row.runnerId,
      runner_name: f?.name ?? row.runnerId,
      kind: f?.kind ?? '',
      rank: i + 1,
      online: f?.status === 'online',
      ready: f?.ready ?? false,
      enabled: row.enabled,
    }
  })
}

export function RunnerAssignments({ agentSlug }: { agentSlug: string }): JSX.Element {
  const [rows, setRows] = useState<AgentRunnerOut[] | null>(null)
  const [fleet, setFleet] = useState<RunnerOut[]>([])
  const [error, setError] = useState<string | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)

  // `rowsRef` always mirrors the latest `rows` the instant it's set — kept in
  // lockstep by `applyRows` below rather than via a `useEffect` (which only
  // catches up after a render/paint). Mutations read this ref, not the `rows`
  // closed over at render time, so a second click fired before the first
  // commit's response lands still computes its next-id list on top of that
  // commit's optimistic update instead of clobbering it.
  const rowsRef = useRef<AgentRunnerOut[]>([])
  const applyRows = (next: AgentRunnerOut[]) => {
    rowsRef.current = next
    setRows(next)
  }

  // Monotonic commit sequence. Two overlapping `putAgentRunners` calls can
  // resolve out of order (or one can error after a later one already
  // succeeded); without this, "last to resolve" would silently win over
  // "last issued", undoing a newer click. Each `commit` call captures the
  // sequence number in effect when it starts and only applies its result
  // (success or error-revert) if no newer commit has since been issued.
  const commitSeqRef = useRef(0)

  useEffect(() => {
    let cancelled = false
    setRows(null)
    rowsRef.current = []
    setError(null)
    Promise.all([getAgentRunners(agentSlug), listRunners()])
      .then(([r, f]) => {
        if (cancelled) return
        applyRows(r)
        setFleet(f)
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setError(e instanceof Error ? e.message : 'Failed to load')
        applyRows([])
      })
    return () => {
      cancelled = true
    }
  }, [agentSlug])

  const assignedIds = useMemo(() => new Set((rows ?? []).map((r) => r.runner_id)), [rows])
  const unassigned = useMemo(
    () => fleet.filter((r) => !assignedIds.has(r.id)),
    [fleet, assignedIds],
  )

  // Every mutation goes through here: compute the next ordered rows list, apply
  // it to local state immediately, then persist — revert to the prior rows on
  // error. `prev` is the pre-mutation snapshot to revert to; it is NOT
  // necessarily the rows currently on screen (a queued second commit's `prev`
  // is the first commit's optimistic result).
  const commit = async (nextRows: RunnerRow[], prev: AgentRunnerOut[]) => {
    const mySeq = ++commitSeqRef.current
    const optimistic = buildOptimisticRows(prev, nextRows, fleet)
    applyRows(optimistic)
    setError(null)
    try {
      const saved = await putAgentRunners(agentSlug, nextRows)
      if (commitSeqRef.current !== mySeq) return // superseded by a newer commit
      applyRows(saved)
    } catch (e: unknown) {
      if (commitSeqRef.current !== mySeq) return // superseded by a newer commit
      applyRows(prev)
      setError(e instanceof Error ? e.message : 'Failed to save')
    }
  }

  const move = (i: number, d: -1 | 1) => {
    const prev = rowsRef.current
    const nextRows = nextRowsForMove(prev, i, d)
    if (nextRows === null) return
    void commit(nextRows, prev)
  }

  // Replaces the old remove: flips enabled, never drops the row.
  const toggle = (id: string) => {
    const prev = rowsRef.current
    void commit(nextRowsForToggle(prev, id), prev)
  }

  const add = (id: string) => {
    const prev = rowsRef.current
    setMenuOpen(false)
    void commit(nextRowsForAdd(prev, id), prev)
  }

  if (rows === null) {
    return <div className="h-8 w-full animate-pulse rounded-md bg-muted" data-testid="runner-assignments-loading" />
  }

  return (
    <div className="flex flex-col gap-2" data-testid={`runner-assignments-${agentSlug}`}>
      <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
        Default order
      </span>
      <div className="flex flex-wrap items-center gap-1.5">
      {rows.length === 0 && (
        <span className="rounded-full border border-warning/30 bg-warning/10 px-2 py-0.5 text-[11px] text-warning">
          unroutable — no runners assigned
        </span>
      )}

      {rows.map((r, i) => (
        <span
          key={r.runner_id}
          className={`flex items-center gap-1 rounded-md border border-border bg-card px-1.5 py-1 ${
            r.enabled ? '' : 'opacity-50'
          }`}
          data-testid={`runner-chip-${r.runner_id}`}
        >
          <span className="w-3.5 text-center text-[10px] font-semibold text-muted-foreground">{i + 1}</span>
          <span
            className={`h-2 w-2 shrink-0 rounded-full ${r.enabled ? dotClass(r) : 'bg-muted-foreground'}`}
            title={r.enabled ? dotTitle(r) : 'disabled'}
          />
          <span
            className={`max-w-[10rem] truncate text-[12px] ${r.enabled ? 'text-foreground' : 'text-muted-foreground'}`}
          >
            {r.runner_name}
          </span>
          <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
            {KIND_GLYPH[r.kind] ?? r.kind}
          </span>
          <button
            type="button"
            onClick={() => move(i, -1)}
            disabled={i === 0}
            className="px-0.5 text-foreground-secondary hover:text-foreground disabled:opacity-30"
            aria-label={`Move ${r.runner_name} up`}
          >
            ↑
          </button>
          <button
            type="button"
            onClick={() => move(i, 1)}
            disabled={i === rows.length - 1}
            className="px-0.5 text-foreground-secondary hover:text-foreground disabled:opacity-30"
            aria-label={`Move ${r.runner_name} down`}
          >
            ↓
          </button>
          {/* No removal affordance — deliberate. This toggles `enabled`
              instead: a disabled runner stays in the list (rank preserved,
              greyed here) but never routes for this agent. */}
          <button
            type="button"
            onClick={() => toggle(r.runner_id)}
            className={`px-0.5 ${r.enabled ? 'text-muted-foreground hover:text-destructive' : 'text-muted-foreground hover:text-success'}`}
            aria-label={r.enabled ? `Disable ${r.runner_name}` : `Enable ${r.runner_name}`}
            title={
              r.enabled
                ? 'disable for this agent'
                : "disabled — won't run for this agent (click to enable)"
            }
          >
            {r.enabled ? '⏻' : '↺'}
          </button>
        </span>
      ))}

      <div className="relative">
        <button
          type="button"
          onClick={() => setMenuOpen((v) => !v)}
          className="rounded-md border border-input bg-input px-2 py-1 text-[11px] text-foreground-secondary hover:border-primary hover:text-primary"
          data-testid="runner-assignments-add-toggle"
        >
          + add
        </button>
        {menuOpen && (
          <div
            className="absolute left-0 top-full z-10 mt-1 flex min-w-[10rem] flex-col gap-0.5 rounded-md border border-border bg-card p-1 shadow-md"
            data-testid="runner-assignments-add-menu"
          >
            {unassigned.length === 0 ? (
              <span className="px-2 py-1 text-[11px] text-muted-foreground">No other runners.</span>
            ) : (
              unassigned.map((r) => (
                <button
                  key={r.id}
                  type="button"
                  onClick={() => add(r.id)}
                  className="flex items-center gap-1.5 rounded px-2 py-1 text-left text-[12px] text-foreground hover:bg-muted"
                >
                  <span
                    className={`h-2 w-2 shrink-0 rounded-full ${
                      r.status !== 'online' ? 'bg-destructive' : r.ready ? 'bg-success' : 'bg-warning'
                    }`}
                  />
                  <span className="min-w-0 flex-1 truncate">{r.name}</span>
                  <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
                    {KIND_GLYPH[r.kind] ?? r.kind}
                  </span>
                </button>
              ))
            )}
          </div>
        )}
      </div>

        {error && <span className="text-[11px] text-destructive">{error}</span>}
      </div>

      {/* The exceptions to the order above — one rule per source. */}
      <RunnerSourceRules agentSlug={agentSlug} fleet={fleet} />
    </div>
  )
}
