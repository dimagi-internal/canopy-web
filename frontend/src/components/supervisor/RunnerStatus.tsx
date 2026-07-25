import type { JSX } from 'react'
import type { RunnerOut } from '@/api/harness'

// Mirrors menubar.py's four derived states (_runner_state, menubar.py:224) so
// the two surfaces read identically — until Phase 5, when the panel loads this
// page and there is only one.
const DOT: Record<string, string> = {
  online: 'bg-success',
  degraded: 'bg-warning',
  stale: 'bg-warning',
  disconnected: 'bg-muted-foreground',
}

function relative(iso: string | null): string {
  if (!iso) return 'never'
  const secs = Math.round((Date.now() - new Date(iso).getTime()) / 1000)
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`
  return `${Math.round(secs / 3600)}h ago`
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
          className="flex w-full items-center gap-2.5 rounded-lg border border-border bg-card px-3 py-2 text-left"
          data-testid={`runner-${r.name}`}
        >
          <span className={`h-2 w-2 shrink-0 rounded-full ${DOT[r.status] ?? 'bg-muted-foreground'}`} />
          <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-foreground">{r.name}</span>
          {!r.ready && (
            <span data-testid={`runner-notready-${r.name}`} className="shrink-0 rounded bg-destructive/15 px-1 text-[10px] text-destructive">
              not ready
            </span>
          )}
          {r.host && <span className="hidden truncate text-[11px] text-foreground-subtle sm:inline">{r.host}</span>}
          <span className="shrink-0 text-[11px] text-muted-foreground">{relative(r.last_heartbeat_at)}</span>
        </button>
      ))}
    </div>
  )
}
