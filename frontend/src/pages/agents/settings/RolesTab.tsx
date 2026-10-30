import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { AgentAccessList } from '@/components/agents/AgentAccessList'
import { AgentLicenses } from '@/components/agents/AgentLicenses'
import { AgentOwnerControl } from '@/components/agents/AgentOwnerControl'
import { Section, Setting } from '@/components/objectPage/settingsLayout'
import { AgentCanopyUserControl } from '@/components/agents/AgentCanopyUserControl'

// Roles: owner, its canopy user, access, who runs it directly.

export function RolesTab({ agent, isAdmin, admins }: { agent: AgentOutletContext['agent']; isAdmin: boolean; admins: string }) {
  return (
    <>
    <Section id="operators" title="Roles">
      <div className="divide-y divide-border rounded-lg border border-border bg-card">
        <Setting
          title="Owner"
          who={`workspace owners and ${agent.name}'s owner`}
          canEdit={agent.can_transfer_owner ?? false}
          description={`Runs ${agent.name}. GitHub features use their GitHub connection.`}
        >
          <AgentOwnerControl
            agentSlug={agent.slug}
            workspace={agent.workspace ?? ''}
            initialOwner={agent.owner ?? null}
            canTransfer={agent.can_transfer_owner ?? false}
          />
        </Setting>
        <Setting
          title="Canopy user"
          who={admins}
          canEdit={isAdmin}
          description={`The canopy account ${agent.name} signs in as.`}
        >
          <AgentCanopyUserControl
            agentSlug={agent.slug}
            workspace={agent.workspace ?? ''}
            initialUser={agent.canopy_user ?? null}
            initialRequest={agent.canopy_user_request ?? null}
            agentEmail={agent.email ?? ''}
            canEdit={isAdmin}
          />
        </Setting>
        <Setting
          title="Access"
          who={`${agent.name}'s agent admins`}
          canEdit={isAdmin}
          description={`Who can use ${agent.name}, and what each may do: viewer, user, editor, then agent admin and owner.`}
        >
          <AgentAccessList
            agentSlug={agent.slug}
            agentName={agent.name}
            workspace={agent.workspace ?? undefined}
            canManageAdmins={agent.can_manage_admins ?? false}
          />
        </Setting>
        <Setting
          title="Direct use"
          who={admins}
          canEdit={isAdmin}
          description={`Who runs ${agent.name} in their own Claude Code, on which computers, and when they last did. There it uses its own tools and secrets, but never its own login; a GitHub or other account lent to it is the person's own there, and acting as them on canopy asks for their passkey.`}
        >
          <AgentLicenses agentSlug={agent.slug} canEdit={isAdmin} />
        </Setting>
      </div>
    </Section>
    </>
  )
}
