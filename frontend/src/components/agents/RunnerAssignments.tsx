import { useEffect, useMemo, useState, type JSX } from 'react'
import { getAgentRunners, putAgentRunners, type AgentRunnerOut } from '@/api/agents'
import { listRunners, type RunnerOut } from '@/api/harness'

// The routing-matrix row: which RUNNERS (not kinds) this agent will route to, in
// rank order. Supersedes RunnerOrder's kind-based `runner_preference` — a rank is
// now a specific paired runner, not a class of runner. Every mutation (reorder,
// remove, add) computes the full ordered id list and PUTs it optimistically
// (local state first, revert on failure) so the row never blocks on a round-trip.

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

export function RunnerAssignments({ agentSlug }: { agentSlug: string }): JSX.Element {
  const [rows, setRows] = useState<AgentRunnerOut[] | null>(null)
  const [fleet, setFleet] = useState<RunnerOut[]>([])
  const [error, setError] = useState<string | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)

  useEffect(() => {
    let cancelled = false
    setRows(null)
    setError(null)
    Promise.all([getAgentRunners(agentSlug), listRunners()])
      .then(([r, f]) => {
        if (cancelled) return
        setRows(r)
        setFleet(f)
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setError(e instanceof Error ? e.message : 'Failed to load')
        setRows([])
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

  // Every mutation goes through here: compute the next ordered id list, apply it
  // to local state immediately, then persist — revert to the prior rows on error.
  const commit = async (nextIds: string[], prev: AgentRunnerOut[]) => {
    // Optimistic reorder of the rows we already know about, keyed by id. Any id
    // not yet in `rows` (a fresh add) is patched in from the fleet list so the
    // chip renders immediately instead of flashing empty until the PUT returns.
    const byId = new Map(prev.map((r) => [r.runner_id, r]))
    const optimistic: AgentRunnerOut[] = nextIds.map((id, i) => {
      const existing = byId.get(id)
      if (existing) return { ...existing, rank: i + 1 }
      const f = fleet.find((r) => r.id === id)
      return {
        runner_id: id,
        runner_name: f?.name ?? id,
        kind: f?.kind ?? '',
        rank: i + 1,
        online: f?.status === 'online',
        ready: f?.ready ?? false,
      }
    })
    setRows(optimistic)
    setError(null)
    try {
      const saved = await putAgentRunners(agentSlug, nextIds)
      setRows(saved)
    } catch (e: unknown) {
      setRows(prev)
      setError(e instanceof Error ? e.message : 'Failed to save')
    }
  }

  const move = (i: number, d: -1 | 1) => {
    const prev = rows ?? []
    const j = i + d
    if (j < 0 || j >= prev.length) return
    const nextIds = prev.map((r) => r.runner_id)
    ;[nextIds[i], nextIds[j]] = [nextIds[j], nextIds[i]]
    void commit(nextIds, prev)
  }

  const remove = (id: string) => {
    const prev = rows ?? []
    void commit(
      prev.filter((r) => r.runner_id !== id).map((r) => r.runner_id),
      prev,
    )
  }

  const add = (id: string) => {
    const prev = rows ?? []
    setMenuOpen(false)
    void commit([...prev.map((r) => r.runner_id), id], prev)
  }

  if (rows === null) {
    return <div className="h-8 w-full animate-pulse rounded-md bg-muted" data-testid="runner-assignments-loading" />
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5" data-testid={`runner-assignments-${agentSlug}`}>
      {rows.length === 0 && (
        <span className="rounded-full border border-warning/30 bg-warning/10 px-2 py-0.5 text-[11px] text-warning">
          unroutable — no runners assigned
        </span>
      )}

      {rows.map((r, i) => (
        <span
          key={r.runner_id}
          className="flex items-center gap-1 rounded-md border border-border bg-card px-1.5 py-1"
          data-testid={`runner-chip-${r.runner_id}`}
        >
          <span className="w-3.5 text-center text-[10px] font-semibold text-muted-foreground">{i + 1}</span>
          <span className={`h-2 w-2 shrink-0 rounded-full ${dotClass(r)}`} title={dotTitle(r)} />
          <span className="max-w-[10rem] truncate text-[12px] text-foreground">{r.runner_name}</span>
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
          <button
            type="button"
            onClick={() => remove(r.runner_id)}
            className="px-0.5 text-muted-foreground hover:text-destructive"
            aria-label={`Remove ${r.runner_name}`}
          >
            ×
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
  )
}
