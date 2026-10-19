import type { JSX } from 'react'
import type { RunnerOut } from '@/api/harness'
import { relativeAge } from '@/lib/relativeAge'
import { isDirectedOnly } from '@/components/chat/runnerEligibility'

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

// Health problems the box reported about itself. A box can be online and ready
// while a feature is off (2026-09-22: no transcripts, no inbox), so this is its
// own chip rather than folded into `not ready`. Red if any check failed, amber
// if only warnings; nothing at all when the box does not report health.
function HealthBadge({ runner }: { runner: RunnerOut }): JSX.Element | null {
  const bad = Object.values(runner.health_checks ?? {}).filter((c) => c.status !== 'ok')
  if (bad.length === 0) return null
  const failing = bad.some((c) => c.status === 'fail')
  return (
    <span
      data-testid={`runner-health-badge-${runner.name}`}
      title={bad.map((c) => c.name).join(', ')}
      className={`shrink-0 rounded px-1 text-[10px] ${
        failing ? 'bg-destructive/15 text-destructive' : 'bg-warning/15 text-warning'
      }`}
    >
      {bad.length} {bad.length === 1 ? 'issue' : 'issues'}
    </span>
  )
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
              {/* Idle sessions on a paused box are not waiting on anybody, so the
                  feed does not nag about them (feed.py: parked needs evidence) —
                  they are counted here, where the pause itself lives. */}
              paused
              {(r.open_sessions ?? 0) > 0
                ? ` · ${r.open_sessions} open session${r.open_sessions === 1 ? '' : 's'}`
                : ''}
            </span>
          ) : !r.ready ? (
            <span data-testid={`runner-notready-${r.name}`} className="shrink-0 rounded bg-destructive/15 px-1 text-[10px] text-destructive">
              not ready
            </span>
          ) : null}
          {/* Health problems the box reported about itself. A box can be online
              and ready while a feature is off (2026-09-22: no transcripts, no
              inbox), so this is its own chip rather than folded into ready. */}
          {/* A test box that takes only what is put on it (pins, chats bound
              to it): a fact about the box, not a problem. */}
          {isDirectedOnly(r) && (
            <span
              data-testid={`runner-directed-${r.name}`}
              className="shrink-0 rounded bg-muted px-1 text-[10px] text-muted-foreground"
              title="Takes only turns pinned to it and chats started on it — no routed work"
            >
              directed only
            </span>
          )}
          <HealthBadge runner={r} />
          {/* The host was `text-foreground-subtle` at 11px — 1.7:1 against this
              row, where AA asks 4.5:1 — AND `hidden sm:inline`, so on a phone it
              was not dim but absent. Which box a runner is on is the field that
              tells two near-identical rows apart, so it is the last thing that
              should be decoration: it moves up the emphasis ladder, up to 12px,
              and onto the second line rather than off the screen. */}
          {r.host && (
            <span className="hidden min-w-0 truncate text-xs text-muted-foreground sm:inline">{r.host}</span>
          )}
          <span className="shrink-0 text-xs text-muted-foreground">{relative(r.last_heartbeat_at)}</span>
          </span>

          {/* Phone: the host the wide row shows inline, on its own line instead
              of hidden. A runner you cannot identify is the failure this list
              exists to prevent. (A readiness-drill badge sat beside it until
              2026-10-12: drills are a debugging tool, and a months-old failure
              painted a healthy box red — they live on the runner's detail.) */}
          {r.host && (
            <span className="flex items-baseline gap-2 pl-[18px] text-xs sm:hidden">
              <span className="min-w-0 truncate text-muted-foreground">{r.host}</span>
            </span>
          )}
        </button>
      ))}
    </div>
  )
}
