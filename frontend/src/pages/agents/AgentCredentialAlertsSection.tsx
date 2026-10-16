import type { AgentCredentialAlertOut } from '@/api/deviceKeys'
import { relativeAge } from '@/lib/relativeAge'

// Read-anomaly alerts (Canopy Vault, canopy-web#1549): reads of this agent's
// secrets that looked wrong. The owner was emailed/pushed for each; at most one
// per kind per hour, with repeats counted. Shown only when there are any.

export const ALERT_LABEL: Record<AgentCredentialAlertOut['kind'], string> = {
  'no-turn': 'Read outside a turn',
  'undeclared-ref': 'Undeclared ref',
  burst: 'Burst of reads',
  'device-proof': 'Device proof refused',
  'zk-wrap-missing': 'Device not granted the vault key',
}

export function AgentCredentialAlertsSection({ alerts }: { alerts: AgentCredentialAlertOut[] | null }) {
  if (!alerts || alerts.length === 0) return null
  return (
    <section id="cred-alerts" className="mb-6" data-testid="agent-credential-alerts">
      <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-warning">Read alerts</h3>
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
