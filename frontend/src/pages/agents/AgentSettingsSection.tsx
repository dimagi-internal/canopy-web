import { useEffect } from 'react'
import { useLocation, useOutletContext, useSearchParams } from 'react-router-dom'
import { CheckCircle2 } from 'lucide-react'

import { AgentAccessRoster } from '@/components/agents/AgentAccessRoster'
import { AgentInterfaceView } from '@/components/agents/AgentInterfaceView'
import { AgentLicenses } from '@/components/agents/AgentLicenses'
import { AgentOwnerControl } from '@/components/agents/AgentOwnerControl'
import { AgentRepositoryControl } from '@/components/agents/AgentRepositoryControl'
import { AgentRouting } from '@/components/agents/AgentRouting'
import { SlackAccessToggle } from '@/components/agents/SlackAccessToggle'
import { AgentMailboxControl } from '@/components/agents/AgentMailboxControl'
import { AgentGoogleAccountControl } from '@/components/agents/AgentGoogleAccountControl'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { AgentCredentialsPanel } from '@/pages/agents/AgentCredentialsPanel'
import { AgentSetupChecklist } from '@/components/agents/AgentSetupChecklist'
import { DeleteAgentControl } from '@/components/agents/DeleteAgentControl'
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
  //
  // Keyed on the navigation (`key`), not just the hash: the setup checklist's
  // "Give it its canopy login" links to `#canopy-login`, and clicking it when the
  // address already ends in that hash changes nothing else. And the target may
  // not exist yet: the credentials sections render after their own data loads,
  // so wait for it (up to ~5s) instead of giving up on the first look.
  const { hash, key } = useLocation()
  // Set by the New agent page on its way here, so the person sees it worked.
  const created = useSearchParams()[0].get('created')
  useEffect(() => {
    if (!hash) return
    const id = decodeURIComponent(hash.slice(1))
    let tries = 0
    const look = (): boolean => {
      const el = document.getElementById(id)
      el?.scrollIntoView?.({ block: 'start' })
      return Boolean(el) || ++tries >= 50
    }
    if (look()) return
    const timer = window.setInterval(() => {
      if (look()) window.clearInterval(timer)
    }, 100)
    return () => window.clearInterval(timer)
  }, [hash, key])

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

      {/* What is left before it can work — computed server-side from the same
          checks a turn meets. Gone once every required step is done. */}
      <div id="setup" className="mb-10 scroll-mt-6">
        {created && (
          <p className="mb-3 flex items-center gap-1.5 text-[13px] text-success" data-testid="agent-created">
            <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
            {agent.name} is created. Its code is in {created}; finish the steps below.
          </p>
        )}
        <AgentSetupChecklist slug={agent.slug} hideWhenDone={!created} />
      </div>

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
          <Setting
            title="Local use"
            who={admins}
            canEdit={isAdmin}
            description={`Who runs ${agent.name} in their own Claude Code, on which private runners, and when they last did.`}
          >
            <AgentLicenses agentSlug={agent.slug} canEdit={isAdmin} />
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
          {/* The checklist's "Give it a repository" lands here (#repository). */}
          <div id="repository" className="scroll-mt-6 border-t border-border">
            <Setting title="Repository" who={admins} canEdit={isAdmin}
                     description="The GitHub repository its runners clone and run, and the branch.">
              <AgentRepositoryControl slug={agent.slug} initialUrl={agent.definition?.repo_url ?? ''}
                                      initialRef={agent.definition?.repo_ref ?? ''} canEdit={isAdmin} />
            </Setting>
          </div>
        </div>
      </Section>

      <Section id="credentials" title="Credentials">
        <AgentCredentialsPanel agent={agent} canEdit={isAdmin} />
      </Section>

      {/* Same gate as the route (delete_agent: agent.work), so only people who can. */}
      {canRoute && (
        <Section id="delete" title="Delete">
          <DeleteAgentControl slug={agent.slug} name={agent.name} workspace={agent.workspace ?? ''} />
        </Section>
      )}
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
