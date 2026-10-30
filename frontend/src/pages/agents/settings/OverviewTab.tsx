import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { CheckCircle2 } from 'lucide-react'
import { AgentHowItRuns } from '@/components/agents/AgentHowItRuns'
import { AgentClisControl } from '@/components/agents/AgentClisControl'
import { AgentRepositoryControl } from '@/components/agents/AgentRepositoryControl'
import { AgentRouting } from '@/components/agents/AgentRouting'
import { AgentHoldControl } from '@/components/agents/AgentHold'
import { AgentSetupChecklist } from '@/components/agents/AgentSetupChecklist'
import { DeleteAgentControl } from '@/components/agents/DeleteAgentControl'
import { Section, Setting } from '@/components/objectPage/settingsLayout'
import { CountStat } from '@/components/agents/cards'

// Overview: what is left to set up, what it is, where it runs, delete.

export function OverviewTab({ agent, isAdmin, canRoute, admins, hold, setHold, created }: {
  agent: AgentOutletContext['agent']
  isAdmin: boolean
  canRoute: boolean
  admins: string
  hold: AgentOutletContext['hold']
  setHold: AgentOutletContext['setHold']
  created: string | null
}) {
  return (
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
  )
}
