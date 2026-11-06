import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { AgentAnsweringSettings } from '@/components/agents/AgentAnsweringSettings'
import { SlackAccessToggle } from '@/components/agents/SlackAccessToggle'
import { AgentMailboxControl } from '@/components/agents/AgentMailboxControl'
import { AgentGoogleAccountControl } from '@/components/agents/AgentGoogleAccountControl'
import { Section, Setting } from '@/components/objectPage/settingsLayout'

// Channels: read mode and host sites, Slack, email, Google account.

export function ChannelsTab({ agent, isAdmin, admins }: { agent: AgentOutletContext['agent']; isAdmin: boolean; admins: string }) {
  return (
    <>
    <Section id="reach" title="Channels">
      <div className="divide-y divide-border rounded-lg border border-border bg-card">
        <Setting
          title="Read mode and host sites"
          who={admins}
          canEdit={isAdmin}
          description={`How ${agent.name} answers people who may read but not drive it, and the sites it can be reached from.`}
        >
          <AgentAnsweringSettings agentSlug={agent.slug} agentName={agent.name} canEdit={isAdmin} />
        </Setting>
        <Setting
          title="Slack"
          who={admins}
          canEdit={isAdmin}
          description={`Whether people can talk to ${agent.name} from the connected Slack.`}
        >
          <SlackAccessToggle agentSlug={agent.slug} initialEnabled={agent.slack_enabled} />
        </Setting>
        <Setting
          title="Email"
          who={admins}
          canEdit={isAdmin}
          description={`The address ${agent.name} sends and reads mail as — a Google mailbox, or a canopy-hosted one (e.g. @dimagi.ai) with nothing to sign in.`}
        >
          <AgentMailboxControl slug={agent.slug} workspace={agent.workspace ?? ''} canEdit={isAdmin} />
        </Setting>
        <Setting
          title="Google account"
          who={`people with the editor role on ${agent.name}, and its admins`}
          canEdit={agent.can_edit_agent ?? false}
          description={`The Google login ${agent.name} uses for Drive, Docs and gog, when it is not the mailbox.`}
        >
          <AgentGoogleAccountControl agent={agent} canEdit={agent.can_edit_agent ?? false} />
        </Setting>
      </div>
    </Section>
    </>
  )
}
