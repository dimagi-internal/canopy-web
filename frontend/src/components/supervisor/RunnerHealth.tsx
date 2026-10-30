import type { JSX } from 'react'
import { Link } from 'react-router-dom'
import type { RunnerOut } from '@/api/harness'
import { relativeAge } from '@/lib/relativeAge'
import { scopedPath } from '../../lib/scopedLinks'

// What the box reports about its own features — one row per check, straight
// from its heartbeat (Runner.health). Exists because "online + ready" said
// nothing on 2026-09-22 while cloud-ec2-1 wrote no transcripts, read no mail
// and could not steer a turn: every one of those was a feature that turned
// itself off with a single journald line. This is where they now show up.
//
// No report is not the same as a clean report: a runner that does not send
// health (a laptop that has not refreshed yet, an older box) renders as "not
// reported", never green. Refreshing it is the runner page's header action, and
// where that request is shows above this card (RunnerRefresh.tsx).

const TONE: Record<string, { dot: string; text: string }> = {
  ok: { dot: 'bg-success', text: 'text-foreground-secondary' },
  warn: { dot: 'bg-warning', text: 'text-warning' },
  fail: { dot: 'bg-destructive', text: 'text-destructive' },
}

// A check that names an agent (`github.<slug>`: the owner has not lent it a
// GitHub token) is fixed on that agent's Settings → External accounts, so the row
// links there. The agent's workspace comes from the host, which already holds
// the agents; without it the row stays text, since the bare /agents/<slug>
// redirect would land in whatever workspace is active rather than the agent's.
export function fixHref(checkName: string, agentWorkspace: (slug: string) => string | undefined): string | null {
  const m = /^github\.(.+)$/.exec(checkName)
  if (!m) return null
  const ws = agentWorkspace(m[1])
  return ws ? scopedPath(ws, `/agents/${m[1]}/settings#cred-github`) : null
}

export function RunnerHealth({
  runner,
  agentWorkspace = () => undefined,
}: {
  runner: RunnerOut
  /** Which workspace an agent lives in, for links to fix a check. */
  agentWorkspace?: (slug: string) => string | undefined
}): JSX.Element | null {
  const reported = runner.health_checks != null
  const cloud = runner.kind === 'cloud'
  if (!reported && !runner.can_administer) return null

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
      </div>
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
                  <td className={`py-1 align-top ${tone.text}`}>
                    {c.detail || c.status}
                    {c.status !== 'ok' && fixHref(c.name, agentWorkspace) && (
                      <Link
                        to={fixHref(c.name, agentWorkspace)!}
                        className="ml-1 whitespace-nowrap text-primary hover:underline"
                        data-testid={`runner-health-fix-${c.name}`}
                      >
                        Fix it →
                      </Link>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      {runner.health_bootstrapped_at ? (
        <p className="text-[11px] text-muted-foreground">
          last {cloud ? 'bootstrap' : 'refresh'} {relativeAge(new Date(runner.health_bootstrapped_at * 1000).toISOString())}
        </p>
      ) : null}
    </div>
  )
}
