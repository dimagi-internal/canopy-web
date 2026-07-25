import { useCallback, useEffect, useState, type JSX } from 'react'
import { startDrill, listDrills, type RunnerDrillOut } from '@/api/drills'
import { relativeTime } from '@/components/activity/turnLog'

// Per-agent readiness grid for one runner — the drill-down behind the
// RunnerStatus badge. Loads the runner's drill outcomes, offers a "Drill
// runner" fan-out button, and self-polls while anything is still pending
// (a drill turn resolves asynchronously via the agent's own report
// callback — see apps/harness/services.py::start_drill).

const POLL_MS = 10_000
const PENDING_TIMEOUT_MS = 30 * 60 * 1000 // 30 minutes

type ChipInfo = { label: string; cls: string }

const CHIP: Record<'pass' | 'fail' | 'pending' | 'timed out', string> = {
  pass: 'bg-success/10 text-success border-success/30',
  fail: 'bg-destructive/10 text-destructive border-destructive/30',
  pending: 'bg-warning/10 text-warning border-warning/30',
  // A pending drill that has sat unresolved for 30+ minutes reads as failed —
  // client-side only, the row's actual `outcome` stays "pending" server-side
  // until a report callback (or the drill turn itself failing) resolves it.
  'timed out': 'bg-destructive/10 text-destructive border-destructive/30',
}

function chipFor(d: RunnerDrillOut, now: Date): ChipInfo {
  if (d.outcome === 'pending') {
    const startedMs = new Date(d.started_at).getTime()
    if (now.getTime() - startedMs > PENDING_TIMEOUT_MS) {
      return { label: 'timed out', cls: CHIP['timed out'] }
    }
    return { label: 'pending', cls: CHIP.pending }
  }
  if (d.outcome === 'pass') return { label: 'pass', cls: CHIP.pass }
  return { label: 'fail', cls: CHIP.fail }
}

function ageFor(d: RunnerDrillOut, now: Date): string {
  return relativeTime(d.finished_at ?? d.started_at, now)
}

// Best-effort extraction of a readable message from the generic Error thrown
// by drills.ts's `unwrap()` — its message embeds the raw problem+json body
// (see apps/api/errors.py) as a JSON blob after "<what> failed: ". Falls
// back to the raw message (or a generic string) when it isn't JSON, so a
// non-JSON failure (a proxy error, a network drop) still shows *something*.
function errorText(e: unknown): string {
  if (!(e instanceof Error)) return 'Drill failed'
  const idx = e.message.indexOf('{')
  if (idx >= 0) {
    try {
      const body = JSON.parse(e.message.slice(idx)) as { detail?: unknown; title?: unknown }
      if (typeof body.detail === 'string' && body.detail.trim()) return body.detail
      if (typeof body.title === 'string' && body.title.trim()) return body.title
    } catch {
      /* not JSON — fall through to the raw message */
    }
  }
  return e.message
}

export function RunnerDrills({ runnerId }: { runnerId: string }): JSX.Element {
  const [drills, setDrills] = useState<RunnerDrillOut[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [drilling, setDrilling] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const rows = await listDrills(runnerId)
      setDrills(rows)
      setError(null)
    } catch (e: unknown) {
      setError(errorText(e))
    }
  }, [runnerId])

  // Initial load + whenever the runner changes.
  useEffect(() => {
    let cancelled = false
    setDrills(null)
    setError(null)
    listDrills(runnerId)
      .then((rows) => {
        if (!cancelled) setDrills(rows)
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setError(errorText(e))
          setDrills([])
        }
      })
    return () => {
      cancelled = true
    }
  }, [runnerId])

  // Self-scheduling poll: while any row is still pending, queue one more
  // `refresh()` 10s out. Re-armed on every `drills` update (including the
  // ones the poll itself produces), and torn down the instant nothing is
  // pending anymore or the component unmounts.
  useEffect(() => {
    if (drills === null) return
    if (!drills.some((d) => d.outcome === 'pending')) return
    const t = setTimeout(() => {
      void refresh()
    }, POLL_MS)
    return () => clearTimeout(t)
  }, [drills, refresh])

  const onDrill = async () => {
    setDrilling(true)
    setError(null)
    try {
      await startDrill(runnerId)
      await refresh()
    } catch (e: unknown) {
      setError(errorText(e))
    } finally {
      setDrilling(false)
    }
  }

  const now = new Date()

  return (
    <div className="flex flex-col gap-2" data-testid="runner-drills">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Readiness drills</span>
        <button
          type="button"
          onClick={() => void onDrill()}
          disabled={drilling}
          className="shrink-0 rounded-md bg-primary px-2 py-1 text-[11px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
          data-testid="drill-runner-button"
        >
          {drilling ? 'Drilling…' : 'Drill runner'}
        </button>
      </div>

      {error && (
        <p className="text-[12px] text-muted-foreground" data-testid="drill-error">
          {error}
        </p>
      )}

      {drills === null ? (
        <div className="h-8 w-full animate-pulse rounded-md bg-muted" data-testid="runner-drills-loading" />
      ) : drills.length === 0 ? (
        <p className="text-[12px] text-muted-foreground">No drills yet.</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="border-b border-border bg-muted text-left text-[10px] uppercase tracking-wide text-muted-foreground">
                <th className="px-2 py-1.5 font-medium">Agent</th>
                <th className="px-2 py-1.5 font-medium">Outcome</th>
                <th className="px-2 py-1.5 font-medium">Age</th>
                <th className="px-2 py-1.5 font-medium">Summary</th>
                <th className="px-2 py-1.5 font-medium">Turn</th>
              </tr>
            </thead>
            <tbody>
              {drills.map((d) => {
                const chip = chipFor(d, now)
                return (
                  <tr key={d.id} className="border-b border-border last:border-b-0" data-testid={`drill-row-${d.id}`}>
                    <td className="px-2 py-1.5 text-foreground">{d.agent_slug}</td>
                    <td className="px-2 py-1.5">
                      <span
                        className={`rounded border px-1.5 py-0.5 text-[10px] ${chip.cls}`}
                        data-testid={`drill-outcome-${d.id}`}
                      >
                        {chip.label}
                      </span>
                    </td>
                    <td className="px-2 py-1.5 text-muted-foreground">{ageFor(d, now)}</td>
                    <td className="max-w-[16rem] truncate px-2 py-1.5 text-foreground-secondary" title={d.summary}>
                      {d.summary || '—'}
                    </td>
                    <td className="px-2 py-1.5 text-muted-foreground">
                      {d.turn_id ? (
                        // No established single-turn-detail route exists yet (the
                        // agent "Turns" rail lists packaged AgentTurns, a
                        // different resource than this harness Turn id) — so
                        // render the short hash with the full id as a tooltip
                        // rather than a dead/misleading link.
                        <span className="cursor-help underline decoration-dotted" title={d.turn_id}>
                          {d.turn_id.slice(0, 8)}
                        </span>
                      ) : (
                        '—'
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
