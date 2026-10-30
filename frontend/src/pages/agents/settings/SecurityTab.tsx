import { useState } from 'react'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { AgentSecretsPanel } from '@/pages/agents/AgentSecretsPanel'
import { AgentSecurityPanel } from '@/pages/agents/AgentSecurityPanel'
import { Section } from '@/components/objectPage/settingsLayout'

// Security: its vault (secrets, who can open them, storage and key), then its canopy login.

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
    </>
  )
}
