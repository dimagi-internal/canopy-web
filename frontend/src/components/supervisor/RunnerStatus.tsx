import type { JSX } from 'react'
import type { RunnerOut } from '@/api/harness'
import type { components } from '@/api/generated'
import { relativeAge } from '@/lib/relativeAge'

type DrillRollup = components['schemas']['DrillRollup']

// Mirrors menubar.py's four derived states (_runner_state, menubar.py:224) so
// the two surfaces read identically — until Phase 5, when the panel loads this
// page and there is only one.
const DOT: Record<string, string> = {
  online: 'bg-success',
  degraded: 'bg-warning',
  stale: 'bg-warning',
  disconnected: 'bg-muted-foreground',
  // A parked box is not a broken one — `info` (deliberate) rather than `warning`
  // (something went wrong). Without its own entry it fell to the grey fallback,
  // rendering identically to a runner that had actually died.
  paused: 'bg-info',
}

// Ladder lives in `relativeAge` (and is unit-tested there) so it cannot stop at
// hours again — that is what produced "drilled 1087h ago" on this very row.
const relative = (iso: string | null): string => relativeAge(iso)

// Worst-signal-wins: a single failed drill outranks any number of pending
// ones for the at-a-glance color, which in turn outranks an all-clear.
function drillBadgeClass(rollup: DrillRollup): string {
  if (rollup.failed > 0) return 'text-destructive'
  if (rollup.pending > 0) return 'text-warning'
  return 'text-success'
}

export function RunnerStatus({
  runners,
  onSelect,
}: {
  runners: RunnerOut[]
  onSelect?: (r: RunnerOut) => void
}): JSX.Element {
  if (runners.length === 0) {
    return (
      <p className="rounded-lg border border-border bg-card p-3 text-[13px] text-muted-foreground">
        No runner paired. Work you queue will wait until one comes online.
      </p>
    )
  }
  return (
    <div className="flex flex-col gap-2" data-testid="runner-status">
      {runners.map((r) => (
        <button
          key={r.id}
          type="button"
          onClick={() => onSelect?.(r)}
          className="flex min-h-11 w-full flex-col gap-1 rounded-lg border border-border bg-card px-3 py-2 text-left sm:min-h-0"
          data-testid={`runner-${r.name}`}
        >
          <span className="flex w-full items-center gap-2.5">
          <span className={`h-2 w-2 shrink-0 rounded-full ${DOT[r.status] ?? 'bg-muted-foreground'}`} />
          <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-foreground">{r.name}</span>
          {/* `paused` outranks `not ready` here for the same reason it outranks a
              pin at claim time: it is the more recent and more specific decision,
              and it explains the silence the other chip would blame on the box. */}
          {r.paused ? (
            <span
              data-testid={`runner-paused-${r.name}`}
              className="shrink-0 rounded bg-info/15 px-1 text-[10px] text-info"
              title={r.paused_note || undefined}
            >
              paused
            </span>
          ) : !r.ready ? (
            <span data-testid={`runner-notready-${r.name}`} className="shrink-0 rounded bg-destructive/15 px-1 text-[10px] text-destructive">
              not ready
            </span>
          ) : null}
          {/* The host was `text-foreground-subtle` at 11px — 1.7:1 against this
              row, where AA asks 4.5:1 — AND `hidden sm:inline`, so on a phone it
              was not dim but absent. Which box a runner is on is the field that
              tells two near-identical rows apart, so it is the last thing that
              should be decoration: it moves up the emphasis ladder, up to 12px,
              and onto the second line rather than off the screen. */}
          {r.host && (
            <span className="hidden min-w-0 truncate text-xs text-muted-foreground sm:inline">{r.host}</span>
          )}
          {r.drill_rollup && (
            <span
              data-testid={`runner-drill-badge-${r.name}`}
              className={`hidden shrink-0 text-xs sm:inline ${drillBadgeClass(r.drill_rollup)}`}
            >
              drilled {relative(r.drill_rollup.last_finished_at)} —{' '}
              {r.drill_rollup.passed}/{r.drill_rollup.passed + r.drill_rollup.failed + r.drill_rollup.pending}
            </span>
          )}
          <span className="shrink-0 text-xs text-muted-foreground">{relative(r.last_heartbeat_at)}</span>
          </span>

          {/* Phone: the two facts the wide row shows inline, on their own line
              instead of hidden. A runner you cannot identify is the failure this
              list exists to prevent. */}
          {(r.host || r.drill_rollup) && (
            <span className="flex items-baseline gap-2 pl-[18px] text-xs sm:hidden">
              {r.host && <span className="min-w-0 truncate text-muted-foreground">{r.host}</span>}
              {r.drill_rollup && (
                <span className={`shrink-0 ${drillBadgeClass(r.drill_rollup)}`}>
                  drilled {relative(r.drill_rollup.last_finished_at)}
                </span>
              )}
            </span>
          )}
        </button>
      ))}
    </div>
  )
}
