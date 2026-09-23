import { useEffect } from 'react'
import { useLocation, useOutletContext } from 'react-router-dom'

import { AgentAdminsControl } from '@/components/agents/AgentAdminsControl'
import { AgentInterfaceView } from '@/components/agents/AgentInterfaceView'
import { AgentOwnerControl } from '@/components/agents/AgentOwnerControl'
import { AgentRouting } from '@/components/agents/AgentRouting'
import { SlackAccessToggle } from '@/components/agents/SlackAccessToggle'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { AgentCredentialsPanel } from '@/pages/agents/AgentCredentialsPanel'
import { Section, Setting } from '@/pages/agents/sectionLayout'
import { CountStat } from '@/components/agents/cards'
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
//   How it runs       — routing: which runner, and which mode, per kind of work
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

      {/* Persona + counts opened Overview. Overview is gone (Work is the
          landing page now) and this is where "what IS this agent" belongs. */}
      <Section id="about" title="About" description={`What ${agent.name} is, and how much it has done.`}>
        {agent.persona && <p className="text-[14px] text-foreground leading-relaxed">{agent.persona}</p>}
        {agent.description && (
          <p className="text-[13px] text-muted-foreground leading-relaxed mt-2">{agent.description}</p>
        )}
        <div className="mt-4 flex flex-wrap gap-6">
          <CountStat value={agent.task_count} label="Tasks" />
          <CountStat value={agent.sync_count} label="Syncs" />
          <CountStat value={agent.work_product_count} label="Work" />
          <CountStat value={agent.skill_count} label="Skills" />
        </div>
      </Section>

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
        description={`Which machine runs each of ${agent.name}'s turns, and whether it may act without asking you first.`}
      >
        <div className="rounded-lg border border-border bg-card">
          {/* Turn mode and runners were two settings; a routing rule can now set
              both ("Beth's email → cloud, auto"), so they are one table whose
              last row is the agent's own defaults (spec 2026-09-23). */}
          <Setting
            title="Routing"
            who="Workspace editors and owners"
            description="Add a rule to send one kind of work, or one person's work, to a different runner or mode."
          >
            <AgentRouting agentSlug={agent.slug} initialTurnMode={agent.turn_mode} />
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
  { id: 'about', title: 'About' },
  { id: 'operators', title: 'Who operates it' },
  { id: 'reach', title: 'Who can reach it' },
  { id: 'running', title: 'How it runs' },
  { id: 'credentials', title: 'Credentials' },
]
