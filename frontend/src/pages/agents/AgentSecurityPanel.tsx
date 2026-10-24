import { useEffect, useState } from 'react'

import { getCredentialAlerts, type AgentCredentialAlertOut } from '@/api/deviceKeys'
import type { ZkStateOut } from '@/api/vault'
import { AgentCredentialAlertsSection } from '@/pages/agents/AgentCredentialAlertsSection'
import { AgentDeviceKeysSection } from '@/pages/agents/AgentDeviceKeysSection'
import { AgentPatRotationSection } from '@/pages/agents/AgentPatRotationSection'
import { AgentTestVaultKeySection } from '@/pages/agents/AgentTestVaultKeySection'
import { AgentVaultMembersSection } from '@/pages/agents/AgentVaultMembersSection'
import { AgentZkSection } from '@/pages/agents/AgentZkSection'

// The bottom of the agent's Security tab, under its secrets: who and what can
// open them — the people and machines holding its vault key, the machines
// approved to read, how its canopy login renews — and reads that looked wrong. Each section is admins-only and
// renders nothing to anyone else.

export function AgentSecurityPanel({
  agent,
  canEdit,
}: {
  agent: { slug: string; name?: string; credential_source?: string | null; require_device_binding?: boolean | null }
  canEdit: boolean
}) {
  const [zk, setZk] = useState<ZkStateOut | null>(null)
  const [alerts, setAlerts] = useState<AgentCredentialAlertOut[] | null>(null)
  useEffect(() => {
    let cancelled = false
    if (!canEdit) return
    getCredentialAlerts(agent.slug).then((a) => !cancelled && setAlerts(a), () => !cancelled && setAlerts([]))
    return () => {
      cancelled = true
    }
  }, [agent.slug, canEdit])

  return (
    <div data-testid="agent-security">
      <AgentCredentialAlertsSection alerts={alerts} />
      <AgentZkSection slug={agent.slug} canEdit={canEdit} onState={setZk} />
      <AgentDeviceKeysSection
        slug={agent.slug}
        credentialSource={agent.credential_source}
        requireDeviceBinding={agent.require_device_binding}
        canEdit={canEdit}
      />
      <AgentVaultMembersSection slug={agent.slug} />
      {!zk?.enabled && <AgentPatRotationSection slug={agent.slug} canEdit={canEdit} />}
      <AgentTestVaultKeySection slug={agent.slug} canEdit={canEdit} />
      {!canEdit && (
        <p className="text-[12px] text-muted-foreground">
          Only {agent.name ?? agent.slug}&rsquo;s admins can see who and what can open its secrets.
        </p>
      )}
    </div>
  )
}
