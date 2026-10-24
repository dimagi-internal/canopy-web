// The small pieces every task screen is built from: a status's and a
// priority's icon, who it is with, its labels — and the one picker for each,
// so changing a task's status is the same click in a table row, on the board
// and on its page.
import { useState, type JSX, type ReactNode } from 'react'
import {
  Bot,
  Check,
  CircleCheck,
  CircleDashed,
  CircleDot,
  CircleSlash,
  Circle,
  Inbox,
  Minus,
  OctagonAlert,
  SignalHigh,
  SignalLow,
  SignalMedium,
  User,
} from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from 'canopy-ui/ui'
import { cn, toneText } from 'canopy-ui/lib'

import type { Label, Task, TaskStatus } from '@/api/work'

import {
  CATEGORY_TONE,
  LABEL_CLASSES,
  PRIORITIES,
  STATUS_CATEGORY,
  STATUS_ORDER,
  displayAgent,
  displayPerson,
  priorityLabel,
  statusName,
  type Category,
} from './meta'
import type { Directory } from './useDirectory'

const CATEGORY_ICON: Record<Category, typeof Circle> = {
  triage: Inbox,
  backlog: CircleDashed,
  unstarted: Circle,
  started: CircleDot,
  completed: CircleCheck,
  canceled: CircleSlash,
}

export function StatusIcon({ status, className }: { status: TaskStatus; className?: string }): JSX.Element {
  const cat = STATUS_CATEGORY[status] ?? 'triage'
  const Icon = CATEGORY_ICON[cat]
  return <Icon aria-hidden="true" className={cn('size-3.5 shrink-0', toneText(CATEGORY_TONE[cat]), className)} />
}

export function PriorityIcon({ priority, className }: { priority: number; className?: string }): JSX.Element {
  const Icon = priority === 1 ? OctagonAlert : priority === 2 ? SignalHigh : priority === 3 ? SignalMedium
    : priority === 4 ? SignalLow : Minus
  return (
    <Icon
      aria-label={priorityLabel(priority)}
      className={cn('size-3.5 shrink-0', priority === 1 ? 'text-destructive' : 'text-muted-foreground', className)}
    />
  )
}

/** Who has it: an agent (bot glyph) or a person (their name), or nobody. */
export function Assignee({ task, compact = false }: { task: Pick<Task, 'agent_slug' | 'assignee_email' | 'assigned'>; compact?: boolean }): JSX.Element {
  if (task.agent_slug) {
    return (
      <span className="inline-flex min-w-0 items-center gap-1 text-foreground-secondary" title={`Agent ${task.agent_slug}`}>
        <Bot aria-hidden="true" className="size-3.5 shrink-0 text-primary" />
        {!compact && <span className="truncate">{displayAgent(task.agent_slug)}</span>}
      </span>
    )
  }
  if (task.assignee_email) {
    return (
      <span className="inline-flex min-w-0 items-center gap-1 text-foreground-secondary" title={task.assignee_email}>
        <User aria-hidden="true" className="size-3.5 shrink-0 text-info" />
        {!compact && <span className="truncate">{displayPerson(task.assignee_email)}</span>}
      </span>
    )
  }
  // Before people could be assigned, a free-text name was all there was.
  return <span className="truncate text-foreground-subtle">{task.assigned?.trim() || (compact ? '' : 'Unassigned')}</span>
}

export function LabelChip({ name, color }: { name: string; color?: string }): JSX.Element {
  return (
    <span
      className={cn(
        'inline-flex h-[18px] items-center rounded border px-1.5 text-[10.5px] font-medium',
        LABEL_CLASSES[color ?? 'muted'] ?? LABEL_CLASSES.muted,
      )}
    >
      {name}
    </span>
  )
}

export function LabelChips({ labels, max = 3 }: { labels: readonly { name: string; color?: string }[]; max?: number }): JSX.Element | null {
  if (!labels.length) return null
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      {labels.slice(0, max).map((l) => (
        <LabelChip key={l.name} name={l.name} color={l.color} />
      ))}
      {labels.length > max && <span className="text-[10.5px] text-muted-foreground">+{labels.length - max}</span>}
    </span>
  )
}

// ---- pickers ------------------------------------------------------------------

/** The one dropdown every picker is: a trigger, a menu, read-only when `disabled`. */
export function Picker({
  trigger,
  label,
  children,
  disabled,
}: {
  trigger: ReactNode
  label: string
  children: ReactNode
  disabled?: boolean
}): JSX.Element {
  if (disabled) return <span className="inline-flex items-center">{trigger}</span>
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label={label}
        className="inline-flex items-center rounded px-0.5 hover:bg-muted"
        onClick={(e: React.MouseEvent) => e.stopPropagation()}
      >
        {trigger}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="max-h-80 min-w-44 overflow-y-auto">
        {children}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function Tick({ on }: { on: boolean }): JSX.Element {
  return <Check aria-hidden="true" className={cn('ml-auto size-3.5', on ? 'opacity-100' : 'opacity-0')} />
}

export function StatusPicker({
  status,
  onChange,
  names,
  disabled,
  children,
}: {
  status: TaskStatus
  onChange: (s: TaskStatus) => void
  names?: Record<string, string> | null
  disabled?: boolean
  children?: ReactNode
}): JSX.Element {
  return (
    <Picker label={`Status: ${statusName(status, names)}`} disabled={disabled} trigger={children ?? <StatusIcon status={status} />}>
      <DropdownMenuLabel>Status</DropdownMenuLabel>
      {STATUS_ORDER.map((s) => (
        <DropdownMenuItem key={s} onClick={() => s !== status && onChange(s)}>
          <StatusIcon status={s} />
          <span>{statusName(s, names)}</span>
          <Tick on={s === status} />
        </DropdownMenuItem>
      ))}
    </Picker>
  )
}

export function PriorityPicker({
  priority,
  onChange,
  disabled,
  children,
}: {
  priority: number
  onChange: (p: number) => void
  disabled?: boolean
  children?: ReactNode
}): JSX.Element {
  return (
    <Picker label={`Priority: ${priorityLabel(priority)}`} disabled={disabled} trigger={children ?? <PriorityIcon priority={priority} />}>
      <DropdownMenuLabel>Priority</DropdownMenuLabel>
      {PRIORITIES.map((p) => (
        <DropdownMenuItem key={p.value} onClick={() => p.value !== priority && onChange(p.value)}>
          <PriorityIcon priority={p.value} />
          <span>{p.label}</span>
          <Tick on={p.value === priority} />
        </DropdownMenuItem>
      ))}
    </Picker>
  )
}

/** `onChange` gets the PATCH value: `agent:<slug>`, an email, or "" (nobody). */
export function AssigneePicker({
  task,
  directory,
  onChange,
  disabled,
  children,
}: {
  task: Pick<Task, 'agent_slug' | 'assignee_email' | 'assigned'>
  directory: Directory
  onChange: (assignee: string) => void
  disabled?: boolean
  children?: ReactNode
}): JSX.Element {
  const current = task.agent_slug ? `agent:${task.agent_slug}` : (task.assignee_email ?? '')
  const pick = (v: string) => v !== current && onChange(v)
  return (
    <Picker label="Assignee" disabled={disabled} trigger={children ?? <Assignee task={task} />}>
      <DropdownMenuItem onClick={() => pick('')}>
        <span className="text-muted-foreground">Unassigned</span>
        <Tick on={current === ''} />
      </DropdownMenuItem>
      {directory.agents.length > 0 && (
        <>
          <DropdownMenuSeparator />
          <DropdownMenuLabel>Agents</DropdownMenuLabel>
          {directory.agents.map((a) => (
            <DropdownMenuItem key={a.slug} onClick={() => pick(`agent:${a.slug}`)}>
              <Bot aria-hidden="true" className="size-3.5 text-primary" />
              <span>{a.name || displayAgent(a.slug)}</span>
              <Tick on={current === `agent:${a.slug}`} />
            </DropdownMenuItem>
          ))}
        </>
      )}
      {directory.people.length > 0 && (
        <>
          <DropdownMenuSeparator />
          <DropdownMenuLabel>People</DropdownMenuLabel>
          {directory.people.map((m) => (
            <DropdownMenuItem key={m.user_id} onClick={() => pick(m.email)}>
              <User aria-hidden="true" className="size-3.5 text-info" />
              <span>{m.email}</span>
              <Tick on={current === m.email} />
            </DropdownMenuItem>
          ))}
        </>
      )}
    </Picker>
  )
}

/** `onChange` gets the whole new list of label names. */
export function LabelPicker({
  labels,
  directory,
  onChange,
  onCreate,
  disabled,
  children,
}: {
  labels: readonly { name: string }[]
  directory: Directory
  onChange: (names: string[]) => void
  onCreate?: (name: string) => void
  disabled?: boolean
  children?: ReactNode
}): JSX.Element {
  const on = new Set(labels.map((l) => l.name.toLowerCase()))
  const [draft, setDraft] = useState('')
  const toggle = (l: Label) => {
    const names = labels.map((x) => x.name)
    onChange(on.has(l.name.toLowerCase()) ? names.filter((n) => n.toLowerCase() !== l.name.toLowerCase()) : [...names, l.name])
  }
  return (
    <Picker label="Labels" disabled={disabled} trigger={children ?? <span className="text-[12px] text-muted-foreground">Labels</span>}>
      <DropdownMenuLabel>Labels</DropdownMenuLabel>
      {directory.labels.map((l) => (
        <DropdownMenuItem key={l.id} closeOnClick={false} onClick={() => toggle(l)}>
          <LabelChip name={l.name} color={l.color} />
          <Tick on={on.has(l.name.toLowerCase())} />
        </DropdownMenuItem>
      ))}
      {onCreate && (
        <div className="border-t border-border p-1">
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              e.stopPropagation()
              if (e.key === 'Enter' && draft.trim()) {
                onCreate(draft.trim())
                setDraft('')
              }
            }}
            placeholder="New label…"
            aria-label="New label"
            className="w-full rounded border border-input bg-input px-1.5 py-0.5 text-[12px] text-foreground placeholder:text-placeholder"
          />
        </div>
      )}
    </Picker>
  )
}

/** `onChange` gets the project's address (`<ws>/<handle>`), or "" for none. */
export function ProjectPicker({
  project,
  directory,
  onChange,
  disabled,
  children,
}: {
  project: string | null | undefined
  directory: Directory
  onChange: (address: string) => void
  disabled?: boolean
  children?: ReactNode
}): JSX.Element {
  return (
    <Picker
      label="Project"
      disabled={disabled}
      trigger={children ?? <span className="truncate text-[12px] text-foreground-secondary">{project || 'No project'}</span>}
    >
      <DropdownMenuItem onClick={() => project && onChange('')}>
        <span className="text-muted-foreground">No project</span>
        <Tick on={!project} />
      </DropdownMenuItem>
      <DropdownMenuSeparator />
      {directory.projects.map((p) => (
        <DropdownMenuItem key={p.id} onClick={() => p.address !== project && onChange(p.address)}>
          <span className="truncate">{p.name}</span>
          <Tick on={p.address === project} />
        </DropdownMenuItem>
      ))}
    </Picker>
  )
}

/** A due date: a native date input, styled to sit in a table row. */
export function DueInput({
  value,
  onChange,
  disabled,
}: {
  value: string | null | undefined
  onChange: (date: string | null) => void
  disabled?: boolean
}): JSX.Element {
  return (
    <input
      type="date"
      value={value ?? ''}
      disabled={disabled}
      aria-label="Due date"
      onClick={(e) => e.stopPropagation()}
      onChange={(e) => onChange(e.target.value || null)}
      className={cn(
        'w-[7.5rem] rounded border border-transparent bg-transparent px-1 text-[12px] text-foreground-secondary hover:border-input focus:border-input disabled:opacity-60',
        // An empty date reads as nothing until you reach for it, not as "mm/dd/yyyy" on every row.
        !value && 'text-transparent hover:text-muted-foreground focus:text-foreground-secondary',
      )}
    />
  )
}
