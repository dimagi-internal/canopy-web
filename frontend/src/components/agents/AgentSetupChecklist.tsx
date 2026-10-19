import { useEffect, useState, type JSX } from 'react'
import { Link } from 'react-router-dom'
import { CheckCircle2, Circle, Clock, MinusCircle, Terminal } from 'lucide-react'

import { getAgentSetup, type AgentSetupStepOut } from '@/api/agentCreation'
import { CopyCommand } from './CopyCommand'

/** The steps left before an agent can do its job, as the server computes them
 *  (`GET /api/agents/{slug}/setup`) — this renders, it never decides. Steps done
 *  on the web link to the control; one done in Claude Code carries the command
 *  that starts that session, and one done in a terminal (it needs the person's
 *  own access, e.g. 1Password rights) carries that command. */
export function AgentSetupChecklist({
  slug,
  hideWhenDone = false,
}: {
  slug: string
  /** Render nothing once every required step is done (the Settings page). */
  hideWhenDone?: boolean
}): JSX.Element | null {
  const [steps, setSteps] = useState<AgentSetupStepOut[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    const load = () =>
      getAgentSetup(slug)
        .then((s) => !cancelled && setSteps(s))
        .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : String(e)))
    void load()
    // Most steps are finished in another tab (GitHub, Google, a terminal), so
    // coming back to this one is when the answer is likely to have changed.
    window.addEventListener('focus', load)
    return () => {
      cancelled = true
      window.removeEventListener('focus', load)
    }
  }, [slug])

  if (error) return <p className="text-[13px] text-destructive">{error}</p>
  if (!steps) return <p className="text-[13px] text-muted-foreground">Checking what is left…</p>
  const left = steps.filter((s) => s.status === 'todo' || s.status === 'waiting').length
  if (hideWhenDone && left === 0) return null

  return (
    <div className="rounded-lg border border-border bg-card" data-testid="agent-setup-checklist">
      <p className="border-b border-border px-4 py-2 text-[12px] text-muted-foreground">
        <span className="font-semibold text-foreground">Setup</span> —{' '}
        {left === 0 ? 'done.' : `${left} of ${steps.length} steps left.`} Ticks update when you come back to this tab.
      </p>
      <SetupSteps steps={steps} />
    </div>
  )
}

/** The steps themselves — shared by the agent's own checklist and the workspace
 *  health dashboard (`/w/:ws/health`), which gets the same steps from the server
 *  for every agent in a tree. `canFix=false` keeps the detail and command but
 *  drops the links: the viewer could not follow them. */
export function SetupSteps({ steps, canFix = true }: { steps: readonly SetupStep[]; canFix?: boolean }): JSX.Element {
  return (
    <ol className="divide-y divide-border">
      {steps.map((step, i) => (
        <li key={step.key} className="flex gap-3 px-4 py-3" data-step={step.key} data-status={step.status}>
          <StatusIcon status={step.status} />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-baseline gap-x-2">
              <span className="text-[13px] font-semibold text-foreground">
                {i + 1}. {step.title}
              </span>
              {step.where !== 'web' && (
                <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
                  <Terminal className="h-3 w-3" aria-hidden="true" />{' '}
                  {step.where === 'claude-code' ? 'in Claude Code' : 'in a terminal'}
                </span>
              )}
            </div>
            <p className="text-[12px] text-foreground-secondary">{step.detail}</p>
            {step.status !== 'done' && step.command && <CopyCommand command={step.command} />}
          </div>
          {canFix && step.status !== 'done' && (step.href || (step.links?.length ?? 0) > 0) && (
            <div className="flex shrink-0 flex-col items-end gap-1 self-start">
              {step.href && <StepLink href={step.href} label={step.action || 'Open'} />}
              {/* Further choices beside the main one, e.g. the runner step's "Pair your laptop". */}
              {(step.links ?? []).map((l) => (
                <StepLink key={l.href} href={l.href} label={l.action} />
              ))}
            </div>
          )}
        </li>
      ))}
    </ol>
  )
}

function StepLink({ href, label }: { href: string; label: string }): JSX.Element {
  const cls = 'shrink-0 self-start text-right text-[12px] text-primary hover:underline'
  return href.startsWith('http') ? (
    <a href={href} target="_blank" rel="noreferrer" className={cls}>
      {label} ↗
    </a>
  ) : (
    <Link to={href} className={cls}>
      {label}
    </Link>
  )
}

/** A step as both the agent's `/setup` and the workspace `/health` serve it. */
export type SetupStep = Pick<AgentSetupStepOut, 'key' | 'title' | 'status' | 'detail' | 'where' | 'href' | 'action' | 'command'> &
  Partial<Pick<AgentSetupStepOut, 'links'>>

export function StatusIcon({ status }: { status: AgentSetupStepOut['status'] }): JSX.Element {
  const cls = 'mt-0.5 h-4 w-4 shrink-0'
  if (status === 'done') return <CheckCircle2 className={`${cls} text-success`} aria-label="done" />
  if (status === 'waiting') return <Clock className={`${cls} text-warning`} aria-label="waiting" />
  if (status === 'optional') return <MinusCircle className={`${cls} text-muted-foreground`} aria-label="optional" />
  return <Circle className={`${cls} text-muted-foreground`} aria-label="to do" />
}
