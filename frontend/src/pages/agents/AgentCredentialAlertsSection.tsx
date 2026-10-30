import type { AgentCredentialAlertOut } from '@/api/deviceKeys'
import { relativeAge } from '@/lib/relativeAge'

// Read-anomaly alerts (Canopy Vault, canopy-web#1549): reads of this agent's
// secrets that looked wrong. The owner was emailed/pushed for each; at most one
// per kind per hour, with repeats counted. Shown only when there are any.

export const ALERT_LABEL: Record<AgentCredentialAlertOut['kind'], string> = {
  'no-turn': 'Read outside a turn',
  'no-turn-check': 'Check run outside a turn',
  'undeclared-ref': 'Asked for a secret it does not declare',
  burst: 'Many reads at once',
  'device-proof': 'A machine could not prove who it is',
  'zk-wrap-missing': 'A machine is not approved for this vault yet',
}

export function AgentCredentialAlertsSection({ alerts }: { alerts: AgentCredentialAlertOut[] | null }) {
  if (!alerts || alerts.length === 0) return null
  return (
    <section id="cred-alerts" data-testid="agent-credential-alerts">
      <AlertRows alerts={alerts} />
    </section>
  )
}

export function AlertRows({ alerts }: { alerts: AgentCredentialAlertOut[] }) {
  return (
    <table className="w-full text-[11px]">
      <tbody className="divide-y divide-border">
        {alerts.map((a) => (
          <tr key={a.id} data-testid={`credential-alert-${a.id}`}>
            <td className="py-1 text-muted-foreground">{relativeAge(a.created_at)}</td>
            <td className="py-1">
              <span className="rounded border border-warning/30 bg-warning/10 px-1.5 py-0.5 text-warning">
                {ALERT_LABEL[a.kind] ?? a.kind}
              </span>
            </td>
            <td className="py-1 text-foreground">{a.detail}</td>
            <td className="py-1 text-muted-foreground">{a.occurrences > 1 ? `${a.occurrences}×` : ''}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
