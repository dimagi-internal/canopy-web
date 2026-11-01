import type { AgentCredentialAlertOut } from '@/api/deviceKeys'
import { relativeAge } from '@/lib/relativeAge'

// Read-anomaly alerts (Canopy Vault, canopy-web#1549): reads of this agent's
// secrets that looked wrong. The owner was emailed/pushed for each; at most one
// per kind per hour, with repeats counted. Shown only when there are any. The
// server keeps one row per hour, so the list is grouped here: the same thing
// asked for again and again reads as one line with a count.

export const ALERT_LABEL: Record<AgentCredentialAlertOut['kind'], string> = {
  'no-turn': 'Read outside a turn',
  'no-turn-check': 'Check run outside a turn',
  'undeclared-ref': 'Asked for a secret it does not declare',
  burst: 'Many reads at once',
  'device-proof': 'A machine could not prove who it is',
  'zk-wrap-missing': 'A machine is not approved for this vault yet',
}

// An agent's own people asking for a name its skills do not list is usually a
// declaration to add, not a sign of misuse; every other kind suggests something is off.
export const isExpected = (kind: AgentCredentialAlertOut['kind']) => kind === 'undeclared-ref'

export interface AlertGroup {
  key: string
  kind: AgentCredentialAlertOut['kind']
  refs: string[]
  /** How many times it happened (each row counts its own repeats). */
  count: number
  /** The newest row: what to show, and when it last happened. */
  latest: AgentCredentialAlertOut
  lastAt: string
}

/** One group per (kind, secrets named), the ones that are not expected first, then newest. */
export function groupAlerts(alerts: readonly AgentCredentialAlertOut[]): AlertGroup[] {
  const groups = new Map<string, AlertGroup>()
  for (const a of alerts) {
    const refs = [...a.refs].sort()
    const key = `${a.kind}|${refs.join(',')}`
    const seen = a.last_seen_at || a.created_at
    const g = groups.get(key)
    if (!g) {
      groups.set(key, { key, kind: a.kind, refs, count: a.occurrences || 1, latest: a, lastAt: seen })
    } else {
      g.count += a.occurrences || 1
      if (seen > g.lastAt) {
        g.lastAt = seen
        g.latest = a
      }
    }
  }
  return [...groups.values()].sort(
    (x, y) => Number(isExpected(x.kind)) - Number(isExpected(y.kind)) || y.lastAt.localeCompare(x.lastAt),
  )
}

export function AgentCredentialAlertsSection({ alerts }: { alerts: AgentCredentialAlertOut[] | null }) {
  if (!alerts || alerts.length === 0) return null
  return (
    <section id="cred-alerts" data-testid="agent-credential-alerts">
      <AlertRows alerts={alerts} />
    </section>
  )
}

function Refs({ refs }: { refs: string[] }) {
  return (
    <>
      {refs.map((r, i) => (
        <span key={r}>
          {i > 0 && ', '}
          <code className="font-mono">{r}</code>
        </span>
      ))}
    </>
  )
}

/** What happened, in a sentence, and what a person can do about it. */
function Sentence({ g }: { g: AlertGroup }) {
  if (g.kind === 'undeclared-ref' && g.refs.length > 0) {
    return (
      <>
        Asked for <Refs refs={g.refs} />, which no skill of this agent lists.
        <span className="text-muted-foreground"> Fine if expected; otherwise add it to what the agent declares.</span>
      </>
    )
  }
  return <>{g.latest.detail}</>
}

export function AlertRows({ alerts }: { alerts: readonly AgentCredentialAlertOut[] }) {
  const groups = groupAlerts(alerts)
  return (
    <table className="w-full text-[11px]">
      <tbody className="divide-y divide-border">
        {groups.map((g) => (
          <tr key={g.key} data-testid={`credential-alert-${g.latest.id}`}>
            <td className="whitespace-nowrap py-1 pr-2 text-muted-foreground">{relativeAge(g.lastAt)}</td>
            <td className="py-1 pr-2">
              <span
                className={`rounded border px-1.5 py-0.5 ${
                  isExpected(g.kind)
                    ? 'border-border bg-muted text-muted-foreground'
                    : 'border-warning/30 bg-warning/10 text-warning'
                }`}
              >
                {ALERT_LABEL[g.kind] ?? g.kind}
              </span>
            </td>
            <td className="py-1 pr-2 text-foreground">
              <Sentence g={g} />
            </td>
            <td className="whitespace-nowrap py-1 text-right text-muted-foreground">{g.count > 1 ? `${g.count}×` : ''}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
