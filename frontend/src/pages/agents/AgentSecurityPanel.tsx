import { VaultSection } from '@/components/vault/VaultSection'
import { AgentCanopyLogin } from '@/pages/agents/AgentCanopyLogin'
import { AgentTestVaultKeySection } from '@/pages/agents/AgentTestVaultKeySection'

// What is about the AGENT, not its vault: how it signs in to canopy (its login, and how
// that renews), and — closed, for testing — the test vault key. The vault (secrets, who
// can open them, storage and key) is `AgentSecretsPanel`, drawn by the shared `VaultView`.

export function AgentSecurityPanel({
  agent,
  canEdit,
  reloadKey = 0,
  onChanged,
}: {
  agent: { slug: string; name?: string }
  canEdit: boolean
  /** Bumped when the vault's secrets changed, so the login re-reads. */
  reloadKey?: number
  /** The login was made or replaced: the vault's table should show it. */
  onChanged?: () => void
}) {
  return (
    <div data-testid="agent-security">
      <AgentCanopyLogin slug={agent.slug} canEdit={canEdit} reloadKey={reloadKey} onChanged={onChanged} />
      <div className="mt-4">
        <VaultSection id="agent-test-key" title="Test vault key" anchors={['cred-test-vault-key']}
          summary="for testing only">
          <AgentTestVaultKeySection slug={agent.slug} canEdit={canEdit} />
          {!canEdit && (
            <p className="text-[12px] text-muted-foreground">Only {agent.name ?? agent.slug}&rsquo;s admins can change this.</p>
          )}
        </VaultSection>
      </div>
    </div>
  )
}
