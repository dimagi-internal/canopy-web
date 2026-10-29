import { useEffect, useState } from 'react'
import { Navigate, useLocation, useNavigate, useOutletContext, useParams, useSearchParams } from 'react-router-dom'
import { CheckCircle2 } from 'lucide-react'

import { getAgentSetup } from '@/api/agentCreation'
import { AgentAccessRoster } from '@/components/agents/AgentAccessRoster'
import { AgentInterfaceView } from '@/components/agents/AgentInterfaceView'
import { AgentHowItRuns } from '@/components/agents/AgentHowItRuns'
import { AgentLicenses } from '@/components/agents/AgentLicenses'
import { AgentOwnerControl } from '@/components/agents/AgentOwnerControl'
import { AgentClisControl } from '@/components/agents/AgentClisControl'
import { AgentRepositoryControl } from '@/components/agents/AgentRepositoryControl'
import { AgentRouting } from '@/components/agents/AgentRouting'
import { AgentHoldControl } from '@/components/agents/AgentHold'
import { SlackAccessToggle } from '@/components/agents/SlackAccessToggle'
import { AgentMailboxControl } from '@/components/agents/AgentMailboxControl'
import { AgentGoogleAccountControl } from '@/components/agents/AgentGoogleAccountControl'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { AgentConnectionsPanel } from '@/pages/agents/AgentConnectionsPanel'
import { AgentAccountsPanel } from '@/pages/agents/AgentAccountsPanel'
import { AgentSecretsPanel } from '@/pages/agents/AgentSecretsPanel'
import { AgentSecurityPanel } from '@/pages/agents/AgentSecurityPanel'
import { AgentSetupChecklist } from '@/components/agents/AgentSetupChecklist'
import { DeleteAgentControl } from '@/components/agents/DeleteAgentControl'
import { ObjectTabs } from '@/components/objectPage/ObjectTabs'
import { Section, Setting } from '@/components/objectPage/settingsLayout'
import {
  isSettingsTab,
  SETTINGS_TABS,
  tabForAnchor,
  tabForStepHref,
  type SettingsTab,
} from '@/pages/agents/settingsTabs'
import { AgentCanopyUserControl } from '@/components/agents/AgentCanopyUserControl'
import { CountStat } from '@/components/agents/cards'
import { WorkbenchSubHeader } from 'canopy-ui'
import { roleAllows } from '@/lib/workspaceRoles'
import { useWorkspaceRole } from '@/workspace/WorkspaceProvider'

// EVERYTHING THAT CONFIGURES AN AGENT, behind tabs (`settings/<tab>`).
//
// It was one long page; changing one key meant scrolling past every other
// setting, and Credentials alone had nine sub-panels. Each tab has its own
// address; old `settings#anchor` links forward to the tab holding the anchor
// (settingsTabs.ts), and the page scrolls to it there.
//
//   Overview           — what is left to set up, what it is, where it runs, delete
//   Roles              — owner, its canopy user, roles, who runs it locally
//   Channels           — callers, Slack, email, Google account
//   External accounts  — GitHub, its Google mailbox
//   Security           — its secrets (the vault), then who and what can open them
export function AgentSettingsSection() {
  const { agent, hold, setHold } = useOutletContext<AgentOutletContext>()
  const { tab } = useParams()
  const { hash, search, key } = useLocation()
  const navigate = useNavigate()
  const isAdmin = agent.is_admin ?? false
  const admins = `${agent.name}'s admins`
  // Routing writes are gated on agent.work (their @gate): workspace editors.
  const canRoute = roleAllows(useWorkspaceRole(agent.workspace), 'agent.work')
  // Set by the New agent page on its way here, so the person sees it worked.
  const created = useSearchParams()[0].get('created')

  // A tab with a setup step left gets a dot, so "what is left" is visible from
  // whichever tab you are on. The server decides what is left; this only maps
  // each step's link to the tab it opens.
  const [needs, setNeeds] = useState<Set<SettingsTab>>(new Set())
  useEffect(() => {
    let cancelled = false
    getAgentSetup(agent.slug)
      .then((steps) => {
        if (cancelled) return
        const open = steps.filter((s) => (s.status === 'todo' || s.status === 'waiting') && !s.only_to_run_on_its_own)
        setNeeds(new Set(open.map((s) => tabForStepHref(s.href)).filter((t): t is SettingsTab => t !== null)))
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [agent.slug])

  const current: SettingsTab | null = isSettingsTab(tab) ? tab : null
  const wanted = hash ? tabForAnchor(hash, search) : null

  // An anchor that lives on another tab (an in-page `#routing` link, a held
  // `#credentials` address) moves to that tab, keeping the anchor.
  useEffect(() => {
    if (current && wanted && wanted !== current) navigate(`../${wanted}${search}${hash}`, { replace: true, relative: 'path' })
  }, [current, wanted, search, hash, navigate])

  // The router does not scroll to a hash by itself. Keyed on the navigation
  // (`key`), not just the hash: clicking a link to the anchor already in the
  // address changes nothing else. And the target may render after its own data
  // loads, so wait for it (up to ~5s) instead of giving up on the first look.
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

  // `settings` itself, or a tab this page does not have: open the tab the
  // anchor belongs to, else Overview.
  if (!current) {
    const to = wanted ?? 'overview'
    return <Navigate to={`${tab ? '..' : '.'}/${to}${search}${hash}`} relative="path" replace />
  }

  return (
    <div className="max-w-5xl px-6 py-8">
      <WorkbenchSubHeader title="Settings" />

      <ObjectTabs
        label="Agent settings"
        relative="path"
        className="-mt-2 mb-6"
        tabs={SETTINGS_TABS.map((t) => ({ to: `../${t.tab}`, label: t.label, dot: needs.has(t.tab), dotLabel: 'needs setting up' }))}
      />

      {current === 'overview' && (
        <>
          {/* What is left before it can work — computed server-side from the
              same checks a turn meets. Gone once every required step is done. */}
          <div id="setup" className="mb-10 scroll-mt-6">
            {created && (
              <p className="mb-3 flex items-center gap-1.5 text-[13px] text-success" data-testid="agent-created">
                <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                {agent.name} is created. Its code is in {created}; finish the steps below.
              </p>
            )}
            <AgentSetupChecklist slug={agent.slug} hideWhenDone={!created} />
          </div>

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

          <Section id="running" title="Where it runs">
            <div className="rounded-lg border border-border bg-card">
              {/* The two ways an agent runs, side by side — a summary that links to
                  the settings below (docs/architecture/runners.md). */}
              <AgentHowItRuns slug={agent.slug} name={agent.name} isAdmin={isAdmin} />
              {/* Turn mode and runners are one table whose last row is the agent's
                  own defaults: a routing rule can set both. */}
              <div id="routing" className="scroll-mt-6 border-t border-border">
                <Setting
                  title="Routing"
                  who="workspace editors and above"
                  canEdit={canRoute}
                  description="Which runner takes each kind of work, and whether it may act without approval."
                >
                  <AgentRouting agentSlug={agent.slug} agentName={agent.name} initialTurnMode={agent.turn_mode} />
                </Setting>
              </div>
              {hold && (
                <div id="hold" className="scroll-mt-6 border-t border-border">
                  <Setting
                    title="Hold"
                    who="workspace editors and above"
                    canEdit={canRoute}
                    description="Stop every runner from starting its turns; they wait queued until you release it."
                  >
                    <AgentHoldControl slug={agent.slug} hold={hold} canEdit={canRoute} onChange={setHold} />
                  </Setting>
                </div>
              )}
              <div id="repository" className="scroll-mt-6 border-t border-border">
                <Setting title="Repository" who={admins} canEdit={isAdmin}
                         description="The GitHub repository its runners clone and run, and the branch.">
                  <AgentRepositoryControl slug={agent.slug} initialUrl={agent.definition?.repo_url ?? ''}
                                          initialRef={agent.definition?.repo_ref ?? ''} canEdit={isAdmin} />
                </Setting>
              </div>
              <div id="clis" className="scroll-mt-6 border-t border-border">
                <Setting title="Agent CLIs" who={admins} canEdit={isAdmin}
                         description="The agent CLIs it is set up for — Claude Code, Codex — its default first, and the model each starts on. A new chat or schedule can pick any of them. A CLI can be added once a runner here has it.">
                  <AgentClisControl slug={agent.slug} initialClis={agent.clis ?? ['claude-code']}
                                    initialModels={agent.cli_models ?? {}} canEdit={isAdmin} />
                </Setting>
              </div>
            </div>
          </Section>

          {/* Same gate as the route (delete_agent: agent.work), so only people who can. */}
          {canRoute && (
            <Section id="delete" title="Delete">
              <DeleteAgentControl slug={agent.slug} name={agent.name} workspace={agent.workspace ?? ''} />
            </Section>
          )}
        </>
      )}

      {/* One line per setting (#1314): a section blurb, a "who can change
          this" label, a description and a footnote were four layers saying
          much the same thing. */}
      {current === 'roles' && (
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
              description={`Who runs ${agent.name} in their own Claude Code, on which computers, and when they last did.`}
            >
              <AgentLicenses agentSlug={agent.slug} canEdit={isAdmin} />
            </Setting>
          </div>
        </Section>
      )}

      {current === 'channels' && (
        <Section id="reach" title="Channels">
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
      )}

      {current === 'accounts' && (
        <Section id="accounts" title="External accounts">
          <AgentAccountsPanel agent={agent} />
          {isAdmin && <AgentConnectionsPanel agent={agent} />}
        </Section>
      )}

      {/* Its secrets first — changing a key is what people come here for —
          then who and what can open them. */}
      {current === 'security' && (
        <>
          <Section id="secrets" title="Secrets">
            <AgentSecretsPanel agent={agent} canEdit={isAdmin} />
          </Section>
          <Section id="access-to-secrets" title="Who can open them">
            <AgentSecurityPanel agent={agent} canEdit={isAdmin} />
          </Section>
        </>
      )}
    </div>
  )
}
