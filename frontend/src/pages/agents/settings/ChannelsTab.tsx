import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { AgentInterfaceView } from '@/components/agents/AgentInterfaceView'
import { SlackAccessToggle } from '@/components/agents/SlackAccessToggle'
import { AgentMailboxControl } from '@/components/agents/AgentMailboxControl'
import { AgentGoogleAccountControl } from '@/components/agents/AgentGoogleAccountControl'
import { Section, Setting } from '@/components/objectPage/settingsLayout'

// Channels: capabilities, Slack, email, Google account.

export function ChannelsTab({ agent, isAdmin, admins }: { agent: AgentOutletContext['agent']; isAdmin: boolean; admins: string }) {
  return (
    <>
    <Section id="reach" title="Channels">
      <div className="divide-y divide-border rounded-lg border border-border bg-card">
        <Setting
          title="Capabilities"
          who={admins}
          canEdit={isAdmin}
          description={`What ${agent.name} offers: the capabilities a viewer or user may use.`}
        >
          <AgentInterfaceView agentSlug={agent.slug} agentName={agent.name} canEdit={isAdmin} />
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
          who={admins}
          canEdit={isAdmin}
          description={`The Google login ${agent.name} uses for Drive, Docs and gog, when it is not the mailbox.`}
        >
          <AgentGoogleAccountControl agent={agent} canEdit={isAdmin} />
        </Setting>
      </div>
    </Section>
    </>
  )
}
