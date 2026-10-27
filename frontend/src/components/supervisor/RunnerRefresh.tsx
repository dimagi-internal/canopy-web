import { useEffect, useState, type JSX } from 'react'
import { refreshRunner, type RunnerOut } from '@/api/harness'
import { relativeAge } from '@/lib/relativeAge'

// A RUNNER'S REFRESH, as the person who pressed it reads it. The button is the
// runner page's header action (it was a small outline button inside the Health
// card); the status panel sits at the top of Overview and says, from
// `RunnerOut.refresh`, where the request is:
//
//   pending — asked when and by whom, and what it waits on: the box is offline,
//             turns are in flight (a cloud box restarts only when idle), or its
//             floor between self-restarts (and when that ends)
//   done    — when it finished, how long it took, the before → after of what the
//             box reports (runner code, Claude Code, the canopy CLI), and any of
//             its refresh checks that did not come back clean
//
// The asker is also emailed and pushed when it finishes; one still pending after
// 30 minutes becomes a `runner_refresh` alert, handed to them.

export type RunnerRefreshState = NonNullable<RunnerOut['refresh']>

/** When a pending refresh is overdue — the alert's threshold, said here too. */
export const STUCK_AFTER_MS = 30 * 60 * 1000

function minutes(seconds: number): string {
  const m = Math.max(1, Math.round(seconds / 60))
  return `${m} min`
}

/** Why a pending refresh has not run, in a person's words. */
export function waitingText(runner: RunnerOut, st: RunnerRefreshState, now: Date = new Date()): string[] {
  const cloud = runner.kind === 'cloud'
  const out: string[] = []
  for (const w of st.waiting_on ?? []) {
    if (w === 'offline') out.push('It is offline: it refreshes when it next comes back and heartbeats.')
    if (w === 'busy') {
      const n = st.in_flight ?? 0
      out.push(`Waiting for ${n} turn${n === 1 ? '' : 's'} in flight to finish — a cloud box restarts only when idle.`)
    }
    if (w === 'floor' && st.eligible_at) {
      out.push(
        `It restarted itself recently and waits at least 30 min between restarts: it can run ${relativeAge(st.eligible_at, now, { future: true, words: true })} (${new Date(st.eligible_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}).`,
      )
    }
  }
  if (out.length === 0) {
    out.push(cloud
      ? 'Nothing is in the way: it restarts at its next heartbeat, updates its plugins, the canopy CLI and Claude Code, then reports back here.'
      : 'Nothing is in the way: it updates its plugins and the canopy CLI at its next heartbeat, then reports back here. Sessions already open keep what they loaded.')
  }
  return out
}

/** The header action: one clearly labelled button, for the people who administer the box. */
export function RunnerRefreshButton({ runner, onChanged }: {
  runner: RunnerOut
  onChanged?: (runner: RunnerOut) => void
}): JSX.Element | null {
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  if (!runner.can_administer) return null
  // The request a person made, not canopy's own CLI-behind ask (`refresh_pending`).
  const pending = runner.refresh?.state === 'pending'
  const what = runner.kind === 'cloud' ? 'plugins, canopy CLI, Claude Code' : 'plugins, canopy CLI'
  const refresh = () => {
    setBusy(true)
    setErr(null)
    refreshRunner(runner.id)
      .then((fresh) => onChanged?.(fresh))
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not request a refresh'))
      .finally(() => setBusy(false))
  }
  return (
    <span className="flex items-center gap-2">
      {err && <span className="text-destructive" data-testid="runner-refresh-error">{err}</span>}
      <button
        type="button"
        onClick={refresh}
        disabled={busy || pending}
        data-testid="runner-refresh"
        title={`Restart into a fresh bootstrap: update its ${what}`}
        className="rounded-md bg-primary px-3 py-1.5 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-60"
      >
        {busy ? 'Asking…' : pending ? 'Refresh pending…' : `Refresh runner — update ${what}`}
      </button>
    </span>
  )
}

/** Where the request is. Nothing when no one has asked. */
export function RunnerRefreshStatus({ runner, now: fixedNow }: { runner: RunnerOut; now?: Date }): JSX.Element | null {
  const st = runner.refresh
  // Ticks the "3 min ago" while it waits; the page polls the runner itself.
  const [tick, setTick] = useState(() => new Date())
  useEffect(() => {
    if (fixedNow || st?.state !== 'pending') return
    const id = setInterval(() => setTick(new Date()), 15_000)
    return () => clearInterval(id)
  }, [fixedNow, st?.state])
  const now = fixedNow ?? tick
  if (!st || st.state === 'none' || !st.requested_at) return null
  const by = st.requested_by ? ` by ${st.requested_by}` : ' by canopy'
  const asked = `Refresh requested ${relativeAge(st.requested_at, now, { words: true })}${by}`

  if (st.state === 'pending') {
    const stuck = now.getTime() - new Date(st.requested_at).getTime() > STUCK_AFTER_MS
    return (
      <div className={`mb-4 rounded-lg border p-3 ${stuck ? 'border-warning/50 bg-warning/5' : 'border-primary/30 bg-primary/5'}`}
        data-testid="runner-refresh-pending">
        <p className="flex items-center gap-2 text-[13px] font-medium text-foreground">
          <span className={`inline-block h-2 w-2 rounded-full ${stuck ? 'bg-warning' : 'animate-pulse bg-primary'}`} />
          {asked} — {stuck ? 'still waiting' : 'pending'}
        </p>
        {waitingText(runner, st, now).map((line) => (
          <p key={line} className="mt-1 text-[12px] text-foreground-secondary" data-testid="runner-refresh-why">{line}</p>
        ))}
        <p className="mt-1 text-[11px] text-muted-foreground">
          {stuck
            ? 'Over 30 minutes: it is on the Alerts list, and whoever asked was told.'
            : st.requested_by
              ? `${st.requested_by} gets an email and a push when it finishes, or if it is still waiting after 30 minutes.`
              : 'This page updates when it finishes.'}
        </p>
      </div>
    )
  }

  const took = st.took_seconds != null ? ` (took ${minutes(st.took_seconds)})` : ''
  const problems = st.problems ?? []
  const changes = st.changes ?? []
  return (
    <div className={`mb-4 rounded-lg border p-3 ${problems.length ? 'border-destructive/40' : 'border-success/40'}`}
      data-testid="runner-refresh-done">
      <p className="flex items-center gap-2 text-[13px] font-medium text-foreground">
        <span className={`inline-block h-2 w-2 rounded-full ${problems.length ? 'bg-destructive' : 'bg-success'}`} />
        Refreshed {st.finished_at ? relativeAge(st.finished_at, now, { words: true }) : ''}{took}
        {problems.length > 0 && <span className="text-destructive">, with problems</span>}
        <span className="text-[11px] font-normal text-muted-foreground">· asked{by}</span>
      </p>
      {changes.length > 0 ? (
        <table className="mt-2 text-[12px]" data-testid="runner-refresh-changes">
          <tbody>
            {changes.map((c) => (
              <tr key={c.name} data-testid={`runner-refresh-change-${c.name}`}>
                <td className="pr-3 text-muted-foreground">{c.name}</td>
                <td className="pr-2 font-mono text-[11px] text-muted-foreground">{c.before || '?'}</td>
                <td className="pr-2 text-muted-foreground">→</td>
                <td className="font-mono text-[11px] text-foreground">{c.after || '?'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="mt-1 text-[12px] text-foreground-secondary">It was already current: nothing it reports changed version.</p>
      )}
      {problems.map((p) => (
        <p key={p} className="mt-1 text-[12px] text-destructive" data-testid="runner-refresh-problem">{p}</p>
      ))}
    </div>
  )
}
