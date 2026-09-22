import { useState, type JSX } from 'react'
import { refreshRunner, type RunnerOut } from '@/api/harness'
import { relativeAge } from '@/lib/relativeAge'

// What the box reports about its own features — one row per check, straight
// from its heartbeat (Runner.health). Exists because "online + ready" said
// nothing on 2026-09-22 while cloud-ec2-1 wrote no transcripts, read no mail
// and could not steer a turn: every one of those was a feature that turned
// itself off with a single journald line. This is where they now show up.
//
// No report is not the same as a clean report: a runner that does not send
// health (the laptops, an older box) renders as "not reported", never green.

const TONE: Record<string, { dot: string; text: string }> = {
  ok: { dot: 'bg-success', text: 'text-foreground-secondary' },
  warn: { dot: 'bg-warning', text: 'text-warning' },
  fail: { dot: 'bg-destructive', text: 'text-destructive' },
}

export function RunnerHealth({
  runner,
  onChanged,
}: {
  runner: RunnerOut
  onChanged?: (runner: RunnerOut) => void
}): JSX.Element | null {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const reported = runner.health_checks != null
  const canRefresh = runner.can_administer && runner.kind === 'cloud'
  if (!reported && !canRefresh) return null

  const refresh = () => {
    setBusy(true)
    setErr(null)
    refreshRunner(runner.id)
      .then((fresh) => onChanged?.(fresh))
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not request a refresh'))
      .finally(() => setBusy(false))
  }

  // Failures first, then warnings, then the rest — the order a person reads in.
  const rank = { fail: 0, warn: 1, ok: 2 } as Record<string, number>
  const checks = Object.values(runner.health_checks ?? {}).sort((a, b) => (rank[a.status] ?? 3) - (rank[b.status] ?? 3))

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3" data-testid="runner-health">
      <div className="flex items-center gap-2">
        <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Health</span>
        <span className="text-[11px] text-muted-foreground" data-testid="runner-health-age">
          {runner.health_received_at ? `reported ${relativeAge(runner.health_received_at)}` : 'not reported'}
        </span>
        {canRefresh && (
          <button
            type="button"
            onClick={refresh}
            disabled={busy || !!runner.refresh_pending}
            data-testid="runner-refresh"
            className="ml-auto rounded-md border border-border px-2.5 py-1 text-[12px] font-medium text-foreground hover:bg-muted disabled:opacity-50"
          >
            {busy ? '…' : runner.refresh_pending ? 'Refresh requested' : 'Refresh'}
          </button>
        )}
      </div>
      {runner.refresh_pending && (
        <p className="text-[12px] text-muted-foreground" data-testid="runner-refresh-pending">
          Waiting for the box to go idle — it restarts, updates its plugins, the canopy CLI and Claude Code,
          then reports back here.
        </p>
      )}
      {checks.length > 0 && (
        <table className="w-full text-[12px]">
          <tbody>
            {checks.map((c) => {
              const tone = TONE[c.status] ?? TONE.ok
              return (
                <tr key={c.name} className="border-b border-border last:border-0" data-testid={`runner-health-${c.name}`}>
                  <td className="w-4 py-1 align-top">
                    <span className={`mt-1.5 inline-block h-2 w-2 rounded-full ${tone.dot}`} />
                  </td>
                  <td className="whitespace-nowrap py-1 pr-3 align-top font-mono text-[11px] text-foreground">{c.name}</td>
                  <td className={`py-1 align-top ${tone.text}`}>{c.detail || c.status}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      {runner.health_bootstrapped_at ? (
        <p className="text-[11px] text-muted-foreground">
          last bootstrap {relativeAge(new Date(runner.health_bootstrapped_at * 1000).toISOString())}
        </p>
      ) : null}
      {err && <p className="text-[12px] text-destructive" data-testid="runner-refresh-error">{err}</p>}
    </div>
  )
}
