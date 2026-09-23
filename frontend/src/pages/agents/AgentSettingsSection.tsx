import { useEffect } from 'react'
import { useLocation, useOutletContext } from 'react-router-dom'

import { AgentAdminsControl } from '@/components/agents/AgentAdminsControl'
import { AgentInterfaceView } from '@/components/agents/AgentInterfaceView'
import { AgentOwnerControl } from '@/components/agents/AgentOwnerControl'
import { RunnerAssignments } from '@/components/agents/RunnerAssignments'
import { SlackAccessToggle } from '@/components/agents/SlackAccessToggle'
import { TurnModeToggle } from '@/components/agents/TurnModeToggle'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { AgentCredentialsPanel } from '@/pages/agents/AgentCredentialsPanel'
import { Section, Setting } from '@/pages/agents/sectionLayout'
import { WorkbenchSubHeader } from 'canopy-ui'

// EVERYTHING THAT CONFIGURES AN AGENT, on one page of its own.
//
// These nine controls were sections of Overview — a page whose other half is a
// read-only dashboard — so "change something about this agent" meant scrolling
// past About, a prompt box and a task summary, and the page had no name that
// said configuration. Credentials had already been folded in for the opposite
// reason (as its own rail entry it was "settings one click away from settings"),
// which fixed the split and left everything in a place nobody would look.
//
// Grouped by the QUESTION each answers, not by which API serves it:
//   Who operates it   — owner, admins
//   Who can reach it  — callers, Slack
//   How it runs       — turn mode, runners
//   Credentials       — the keys, and the vault they are read from
export function AgentSettingsSection() {
  const { agent } = useOutletContext<AgentOutletContext>()

  // `#credentials` is a link people hold: the Google mailbox mint returns to it,
  // and it was the old Credentials page's address twice over. The router does
  // not scroll to a hash by itself.
  const { hash } = useLocation()
  useEffect(() => {
    if (!hash) return
    const el = document.getElementById(decodeURIComponent(hash.slice(1)))
    el?.scrollIntoView?.({ block: 'start' })
  }, [hash])

  return (
    <div className="max-w-4xl px-6 py-8">
      <WorkbenchSubHeader title="Settings" />

      <nav aria-label="On this page" className="-mt-2 mb-8 flex flex-wrap gap-x-4 gap-y-1 text-[12px]">
        {SECTIONS.map((sec) => (
          <a key={sec.id} href={`#${sec.id}`} className="text-muted-foreground transition-colors hover:text-primary">
            {sec.title}
          </a>
        ))}
      </nav>

      <Section
        id="operators"
        title="Who operates it"
        description={`The people accountable for ${agent.name} and trusted with its keys.`}
      >
        <div className="divide-y divide-border rounded-lg border border-border bg-card">
          <Setting
            title="Owner"
            who="Workspace owners and the current owner"
            description={`The person who operates ${agent.name}. Its GitHub-backed features, including History, read the repository through this person's GitHub connection.`}
          >
            <AgentOwnerControl
              agentSlug={agent.slug}
              workspace={agent.workspace ?? ''}
              initialOwner={agent.owner ?? null}
              canTransfer={agent.can_transfer_owner ?? false}
            />
          </Setting>
          <Setting
            title="Admins"
            who="The agent's owner and workspace owners"
            description={`People trusted with all of ${agent.name}: they can set its credentials. Everyone else in the workspace can use it but not hold its keys.`}
          >
            <AgentAdminsControl
              agentSlug={agent.slug}
              workspace={agent.workspace ?? ''}
              canManage={agent.can_manage_admins ?? false}
            />
          </Setting>
        </div>
      </Section>

      <Section
        id="reach"
        title="Who can reach it"
        description={`Who may ask ${agent.name} for something, and through which door.`}
      >
        <div className="divide-y divide-border rounded-lg border border-border bg-card">
          <Setting
            title="Callers"
            who="The agent's owner and admins"
            description={`Who else may use ${agent.name}, and for what: the whole agent for addresses you trust (e.g. everyone at your domain), a confined capability for everyone else. Each also appears as an MCP tool.`}
          >
            <AgentInterfaceView agentSlug={agent.slug} canEdit={agent.is_admin ?? false} />
          </Setting>
          <Setting
            title="Slack"
            who="Workspace owners"
            description={`Whether people can talk to ${agent.name} from the connected Slack — by @mention, DM, or /canopy ${agent.slug}. Members act as themselves; anyone else is answered as a contact.`}
          >
            <SlackAccessToggle agentSlug={agent.slug} initialEnabled={agent.slack_enabled} />
          </Setting>
        </div>
      </Section>

      <Section
        id="running"
        title="How it runs"
        description={`What ${agent.name} may do in a turn, and which machines execute it.`}
      >
        <div className="divide-y divide-border rounded-lg border border-border bg-card">
          <Setting
            title="Turn mode"
            who="Workspace editors and owners"
            description={`How ${agent.name}'s turns handle outbound actions. Read at the start of every turn.`}
          >
            <TurnModeToggle agentSlug={agent.slug} initialMode={agent.turn_mode} />
          </Setting>
          <Setting
            title="Runners"
            who="Workspace editors and owners"
            description={`Which runners execute ${agent.name}'s turns, in order. The top online and ready runner claims first.`}
          >
            <RunnerAssignments agentSlug={agent.slug} />
          </Setting>
        </div>
      </Section>

      <Section
        id="credentials"
        title="Credentials"
        description={`The secrets ${agent.name} needs to run, and whether each is set. Anyone here can see the status; only the agent's owner and admins can change a value.`}
      >
        <AgentCredentialsPanel agent={agent} />
      </Section>
    </div>
  )
}

const SECTIONS: { id: string; title: string }[] = [
  { id: 'operators', title: 'Who operates it' },
  { id: 'reach', title: 'Who can reach it' },
  { id: 'running', title: 'How it runs' },
  { id: 'credentials', title: 'Credentials' },
]
