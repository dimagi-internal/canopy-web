// The pure half of the runner list: the dot and its title, and the next list of rows after a move, toggle, add or optimistic update.
import { type AgentRunnerOut } from '@/api/agents'
import { type RunnerOut } from '@/api/harness'

export function dotClass(r: AgentRunnerOut): string {
  if (!r.online) return 'bg-destructive'
  return r.ready ? 'bg-success' : 'bg-warning'
}

export function dotTitle(r: AgentRunnerOut): string {
  if (!r.online) return 'offline'
  return r.ready ? 'online — ready' : 'online — not ready'
}

// The rows form PUT sends: {runnerId, enabled}, index = rank. Pure
// row-list transforms, extracted so the logic is unit-testable without a
// React renderer (see RunnerAssignments.test.tsx).
export type RunnerRow = { runnerId: string; enabled: boolean }

export function toRows(rows: readonly AgentRunnerOut[]): RunnerRow[] {
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
