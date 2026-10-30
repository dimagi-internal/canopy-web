import { useEffect, useState } from 'react'

import { getAgentCredentialStatus } from '@/api/agents'
import { getPatRotation } from '@/api/deviceKeys'
import { VaultSection } from '@/components/vault/VaultSection'
import { AgentPatRotationSection } from '@/pages/agents/AgentPatRotationSection'
import { AgentSecretsHomeSection } from '@/pages/agents/AgentSecretsHomeSection'
import { AgentTestVaultKeySection } from '@/pages/agents/AgentTestVaultKeySection'

// The rest of the agent's Security tab: what is about the AGENT, not its vault. The
// vault (secrets, who can open them, storage and key) is `AgentSecretsPanel`, drawn
// by the shared `VaultView`. Here: how the agent signs in to canopy (its login, and
// how that renews) and the test vault key. Each is one closed line until opened.

const LOGIN_REF = 'CANOPY_WEB_PAT'

export function AgentSecurityPanel({
  agent,
  canEdit,
  reloadKey = 0,
  onChanged,
}: {
  agent: { slug: string; name?: string }
  canEdit: boolean
  /** Bumped when the vault's secrets changed, so the login line re-reads. */
  reloadKey?: number
  /** The login was made or renewed: the vault's table should show it. */
  onChanged?: () => void
}) {
  const [hasLogin, setHasLogin] = useState<boolean | null>(null)
  const [renews, setRenews] = useState<number | null>(null)

  useEffect(() => {
    let cancelled = false
    getAgentCredentialStatus(agent.slug)
      .then((rows) => !cancelled && setHasLogin(rows.some((r) => r.name === LOGIN_REF && r.set)), () => undefined)
    if (canEdit) {
      getPatRotation(agent.slug).then((r) => !cancelled && setRenews(r.enabled ? r.period_days : 0), () => undefined)
    }
    return () => {
      cancelled = true
    }
  }, [agent.slug, canEdit, reloadKey])

  const loginSummary = hasLogin === null
    ? undefined
    : !hasLogin
      ? 'not made yet'
      : renews ? `stored · renews every ${renews} days` : 'stored · does not renew on its own'

  return (
    <div data-testid="agent-security">
      <VaultSection id="agent-login" title="Canopy login" summary={loginSummary}
        tone={hasLogin === false ? 'attention' : 'quiet'} anchors={['canopy-login', 'cred-pat-rotation']}>
        <AgentSecretsHomeSection
          slug={agent.slug}
          storedNames={hasLogin ? [LOGIN_REF] : []}
          canEdit={canEdit}
          onChanged={onChanged}
          show="login"
        />
        <AgentPatRotationSection slug={agent.slug} canEdit={canEdit} />
      </VaultSection>
      <VaultSection id="agent-test-key" title="Test vault key" anchors={['cred-test-vault-key']}
        summary={canEdit ? 'for testing only' : 'admins only'}>
        <AgentTestVaultKeySection slug={agent.slug} canEdit={canEdit} />
        {!canEdit && (
          <p className="text-[12px] text-muted-foreground">Only {agent.name ?? agent.slug}&rsquo;s admins can change this.</p>
        )}
      </VaultSection>
    </div>
  )
}
