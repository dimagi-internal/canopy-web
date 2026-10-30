import { type ReactNode } from 'react'
import { PasskeysPanel } from './PasskeysPanel'
import { RecoveryRow } from './RecoveryRow'
import { ZkAgentsPanel } from './ZkAgentsPanel'

/**
 * Security — one card for everything that keeps agents' secrets safe
 * (canopy-web#1550): You (how you confirm it's you, your recovery code),
 * Your runners (approve or revoke), Your agents (where each stands). The old
 * separate Passkeys / Device keys / Vault key / Zero-knowledge cards were one
 * idea split four ways. Deep links still land: #passkeys, #vault-key,
 * #device-keys, #your-agents, and #your-machines (this row's old name, in
 * links already sent).
 */
export function SecurityCard({ machines }: { machines: ReactNode }) {
  return (
    <div id="security" className="rounded-xl border border-border bg-card p-5 space-y-5" data-testid="settings-security">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Security</h2>
        <p className="mt-1 text-sm text-foreground-secondary" data-testid="security-summary">
          Your agents&rsquo; secrets can be opened by: <strong className="font-medium text-foreground">you</strong> (your
          passkey) · <strong className="font-medium text-foreground">runners you approve</strong> · not canopy-web.
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          canopy-web stores each agent&rsquo;s secrets locked. A copy of the key goes to you and to each runner you
          approve; a runner&rsquo;s copy is kept inside its own hardware and can&rsquo;t be copied off it.
        </p>
      </div>

      <Row id="you" title="You">
        <PasskeysPanel />
        <RecoveryRow />
      </Row>

      <Row id="your-runners" title="Your runners"
           hint="Each can open the agents it runs. Approving a runner — one passkey tap — hands it its copy of the key.">
        <span id="your-machines" aria-hidden="true" />
        {machines}
      </Row>

      <Row id="your-agents" title="Your agents">
        <ZkAgentsPanel />
      </Row>
    </div>
  )
}

function Row({ id, title, hint, children }: { id: string; title: string; hint?: string; children: ReactNode }) {
  return (
    <section id={id} className="space-y-2 border-t border-border pt-4 first-of-type:border-t-0 first-of-type:pt-0"
             data-testid={`security-${id}`}>
      <h3 className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{title}</h3>
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
      {children}
    </section>
  )
}
