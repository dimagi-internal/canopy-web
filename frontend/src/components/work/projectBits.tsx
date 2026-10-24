// The small pieces a project is shown with — its status and health chips, a
// done/total bar — and the pickers its header edits it with: a lead person, a
// lead agent, the agents working on it. One of each, so the Projects table,
// the agent's Projects tab and the project page say the same thing.
import type { JSX, ReactNode } from 'react'
import { Bot, User } from 'lucide-react'
import { DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, StatusBadge } from 'canopy-ui/ui'

import type { Project } from '@/api/work'

import { Picker, Tick } from './bits'
import { displayAgent, displayPerson, healthMeta, projectStatusMeta } from './meta'
import type { Directory } from './useDirectory'

export function ProjectStatusBadge({ status }: { status: string }): JSX.Element {
  const m = projectStatusMeta(status)
  return <StatusBadge tone={m.tone}>{m.label}</StatusBadge>
}

/** The latest published update's health, or "Update missing" when one is due. */
export function HealthBadge({
  project,
}: {
  project: Pick<Project, 'health' | 'update_missing'>
}): JSX.Element | null {
  if (project.update_missing) {
    return (
      <StatusBadge tone="warning" dot>
        Update missing
      </StatusBadge>
    )
  }
  const h = healthMeta(project.health)
  if (!h) return null
  return (
    <StatusBadge tone={h.tone} dot>
      {h.label}
    </StatusBadge>
  )
}

/** done of total, as a thin bar and the numbers. */
export function ProgressBar({ done, total, className = '' }: { done: number; total: number; className?: string }): JSX.Element {
  const pct = total > 0 ? Math.round((done / total) * 100) : 0
  return (
    <span className={`inline-flex min-w-0 items-center gap-2 ${className}`} title={`${done} of ${total} done`}>
      <span
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={done}
        aria-label={`${done} of ${total} done`}
        className="h-1.5 w-16 shrink-0 overflow-hidden rounded-full bg-muted"
      >
        <span className="block h-full rounded-full bg-success" style={{ width: `${pct}%` }} />
      </span>
      <span className="whitespace-nowrap text-[11.5px] tabular-nums text-muted-foreground">
        {done}/{total}
      </span>
    </span>
  )
}

export function AgentName({ slug }: { slug: string }): JSX.Element {
  return (
    <span className="inline-flex min-w-0 items-center gap-1 text-foreground-secondary" title={`Agent ${slug}`}>
      <Bot aria-hidden="true" className="size-3.5 shrink-0 text-primary" />
      <span className="truncate">{displayAgent(slug)}</span>
    </span>
  )
}

export function PersonName({ email }: { email: string }): JSX.Element {
  return (
    <span className="inline-flex min-w-0 items-center gap-1 text-foreground-secondary" title={email}>
      <User aria-hidden="true" className="size-3.5 shrink-0 text-info" />
      <span className="truncate">{displayPerson(email)}</span>
    </span>
  )
}

/** A project's lead: one of the workspace's people, or nobody (""). */
export function PersonPicker({
  email,
  directory,
  onChange,
  disabled,
  label = 'Lead',
}: {
  email: string | null | undefined
  directory: Directory
  onChange: (email: string) => void
  disabled?: boolean
  label?: string
}): JSX.Element {
  const current = email ?? ''
  const trigger: ReactNode = current ? <PersonName email={current} /> : <span className="text-foreground-subtle">No lead</span>
  return (
    <Picker label={label} disabled={disabled} trigger={trigger}>
      <DropdownMenuItem onClick={() => current && onChange('')}>
        <span className="text-muted-foreground">No lead</span>
        <Tick on={!current} />
      </DropdownMenuItem>
      {directory.people.length > 0 && <DropdownMenuSeparator />}
      {directory.people.map((m) => (
        <DropdownMenuItem key={m.user_id} onClick={() => m.email !== current && onChange(m.email)}>
          <User aria-hidden="true" className="size-3.5 text-info" />
          <span>{m.email}</span>
          <Tick on={m.email === current} />
        </DropdownMenuItem>
      ))}
    </Picker>
  )
}

/** A project's lead agent (it holds the Drive folder), or none (""). */
export function AgentPicker({
  slug,
  directory,
  onChange,
  disabled,
}: {
  slug: string | null | undefined
  directory: Directory
  onChange: (slug: string) => void
  disabled?: boolean
}): JSX.Element {
  const current = slug ?? ''
  const trigger: ReactNode = current ? <AgentName slug={current} /> : <span className="text-foreground-subtle">No lead agent</span>
  return (
    <Picker label="Lead agent" disabled={disabled} trigger={trigger}>
      <DropdownMenuItem onClick={() => current && onChange('')}>
        <span className="text-muted-foreground">No lead agent</span>
        <Tick on={!current} />
      </DropdownMenuItem>
      {directory.agents.length > 0 && <DropdownMenuSeparator />}
      {directory.agents.map((a) => (
        <DropdownMenuItem key={a.slug} onClick={() => a.slug !== current && onChange(a.slug)}>
          <Bot aria-hidden="true" className="size-3.5 text-primary" />
          <span>{a.name || displayAgent(a.slug)}</span>
          <Tick on={a.slug === current} />
        </DropdownMenuItem>
      ))}
    </Picker>
  )
}

/** The agents working on a project beside its lead: `onChange` gets the whole new list. */
export function AgentsPicker({
  slugs,
  lead,
  directory,
  onChange,
  disabled,
}: {
  slugs: readonly string[]
  lead?: string | null
  directory: Directory
  onChange: (slugs: string[]) => void
  disabled?: boolean
}): JSX.Element {
  const others = slugs.filter((s) => s !== lead)
  const on = new Set(slugs)
  const trigger: ReactNode = others.length ? (
    <AgentList slugs={others} />
  ) : (
    <span className="text-foreground-subtle">No other agents</span>
  )
  return (
    <Picker label="Agents" disabled={disabled} trigger={trigger}>
      <DropdownMenuLabel>Agents working on it</DropdownMenuLabel>
      {directory.agents
        .filter((a) => a.slug !== lead)
        .map((a) => (
          <DropdownMenuItem
            key={a.slug}
            closeOnClick={false}
            onClick={() => onChange(on.has(a.slug) ? slugs.filter((s) => s !== a.slug) : [...slugs, a.slug])}
          >
            <Bot aria-hidden="true" className="size-3.5 text-primary" />
            <span>{a.name || displayAgent(a.slug)}</span>
            <Tick on={on.has(a.slug)} />
          </DropdownMenuItem>
        ))}
    </Picker>
  )
}

/** A few agents in a line: the first `max` by name, then "+n". */
export function AgentList({ slugs, max = 3 }: { slugs: readonly string[]; max?: number }): JSX.Element {
  return (
    <span className="inline-flex min-w-0 items-center gap-1 text-foreground-secondary" title={slugs.join(', ')}>
      <Bot aria-hidden="true" className="size-3.5 shrink-0 text-primary" />
      <span className="truncate">{slugs.slice(0, max).map(displayAgent).join(', ')}</span>
      {slugs.length > max && <span className="shrink-0 text-[11px] text-muted-foreground">+{slugs.length - max}</span>}
    </span>
  )
}
