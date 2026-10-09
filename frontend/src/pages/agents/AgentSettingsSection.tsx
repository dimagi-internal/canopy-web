import { useEffect } from 'react'
import { useLocation, useOutletContext } from 'react-router-dom'

import { AgentAccessRoster } from '@/components/agents/AgentAccessRoster'
import { AgentInterfaceView } from '@/components/agents/AgentInterfaceView'
import { AgentOwnerControl } from '@/components/agents/AgentOwnerControl'
import { AgentRouting } from '@/components/agents/AgentRouting'
import { SlackAccessToggle } from '@/components/agents/SlackAccessToggle'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { AgentCredentialsPanel } from '@/pages/agents/AgentCredentialsPanel'
import { Section, Setting } from '@/pages/agents/sectionLayout'
import { AgentCanopyUserControl } from '@/components/agents/AgentCanopyUserControl'
import { CountStat } from '@/components/agents/cards'
import { WorkbenchSubHeader } from 'canopy-ui'
import { roleAllows } from '@/lib/workspaceRoles'
import { useWorkspaceRole } from '@/workspace/WorkspaceProvider'

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
//   People and roles  — owner, and everyone's role here (admins granted inline)
//   Who can reach it  — callers, Slack
//   How it runs       — routing: which runner, and which mode, per kind of work
//   Credentials       — the keys, and the vault they are read from
export function AgentSettingsSection() {
  const { agent } = useOutletContext<AgentOutletContext>()
  const isAdmin = agent.is_admin ?? false
  const admins = `${agent.name}'s admins`
  // Routing writes are gated on agent.work (route_gates.py): workspace editors.
  const canRoute = roleAllows(useWorkspaceRole(agent.workspace), 'agent.work')

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
      <Section id="about" title="About">
        {agent.persona && <p className="text-[14px] text-foreground leading-relaxed">{agent.persona}</p>}
        {agent.description && (
          <p className="text-[13px] text-muted-foreground leading-relaxed mt-2">{agent.description}</p>
        )}
        <div className="mt-4 flex flex-wrap gap-6">
          <CountStat value={agent.task_count} label="Tasks" />
          <CountStat value={agent.sync_count} label="Syncs" />
          <CountStat value={agent.skill_count} label="Skills" />
        </div>
      </Section>

      {/* One line per setting (#1314): a section blurb, a "who can change
          this" label, a description and a footnote were four layers saying
          much the same thing. */}
      <Section id="operators" title="People and roles">
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
              canEdit={isAdmin}
            />
          </Setting>
          <Setting
            title="People"
            who={`workspace owners and ${agent.name}'s owner`}
            canEdit={agent.can_manage_admins ?? false}
            description={`What each workspace role gets on ${agent.name}, and who is set here.`}
          >
            <AgentAccessRoster
              agentSlug={agent.slug}
              agentName={agent.name}
              workspace={agent.workspace ?? undefined}
              canManage={agent.can_manage_admins ?? false}
            />
          </Setting>
        </div>
      </Section>

      <Section id="reach" title="Who can reach it">
        <div className="divide-y divide-border rounded-lg border border-border bg-card">
          <Setting
            title="Callers"
            who={admins}
            canEdit={isAdmin}
            description={`Who outside the workspace may use ${agent.name}, and for what.`}
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
        </div>
      </Section>

      <Section id="running" title="How it runs">
        <div className="rounded-lg border border-border bg-card">
          {/* Turn mode and runners were two settings; a routing rule can now set
              both ("Beth's email → cloud, auto"), so they are one table whose
              last row is the agent's own defaults (spec 2026-09-23). */}
          <Setting
            title="Routing"
            who="workspace editors and above"
            canEdit={canRoute}
            description="Which runner takes each kind of work, and whether it may act without approval."
          >
            <AgentRouting agentSlug={agent.slug} agentName={agent.name} initialTurnMode={agent.turn_mode} />
          </Setting>
        </div>
      </Section>

      <Section id="credentials" title="Credentials">
        <AgentCredentialsPanel agent={agent} canEdit={isAdmin} />
      </Section>
    </div>
  )
}

const SECTIONS: { id: string; title: string }[] = [
  { id: 'about', title: 'About' },
  { id: 'operators', title: 'People and roles' },
  { id: 'reach', title: 'Who can reach it' },
  { id: 'running', title: 'How it runs' },
  { id: 'credentials', title: 'Credentials' },
]
