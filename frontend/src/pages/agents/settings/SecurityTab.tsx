import { useState } from 'react'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { AgentBrowserSignInSection } from '@/pages/agents/AgentBrowserSignInSection'
import { AgentSecretsPanel } from '@/pages/agents/AgentSecretsPanel'
import { AgentSecurityPanel } from '@/pages/agents/AgentSecurityPanel'
import { Section } from '@/components/objectPage/settingsLayout'

// Security: its vault (secrets, who can open them, storage and key), its canopy login, and —
// off unless an admin switches it on — browser sign-in as the agent.

export function SecurityTab({ agent, isAdmin }: { agent: AgentOutletContext['agent']; isAdmin: boolean }) {
  // A counter the canopy login bumps so the vault's table re-reads.
  const [vaultVersion, setVaultVersion] = useState(0)
  return (
    <>
    <Section id="vault" title="Vault">
      <AgentSecretsPanel
        agent={agent}
        canEdit={isAdmin}
        reloadKey={vaultVersion}
      />
    </Section>
    <Section id="canopy-login-section" title="Canopy login">
      <AgentSecurityPanel
        agent={agent}
        canEdit={isAdmin}
        reloadKey={vaultVersion}
        onChanged={() => setVaultVersion((v) => v + 1)}
      />
    </Section>
    <Section id="browser-sign-in-section" title="Browser sign-in">
      <AgentBrowserSignInSection slug={agent.slug} canEdit={isAdmin} />
      {!isAdmin && (
        <p className="text-[12px] text-muted-foreground">Only {agent.name ?? agent.slug}&rsquo;s admins can change this.</p>
      )}
    </Section>
    </>
  )
}
