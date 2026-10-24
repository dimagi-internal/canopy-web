// What every task and project screen says about a status, a priority and a
// health — one table each, so the words and tones cannot drift between the
// list, the board, the peek and the project page. Logic reads the CATEGORY
// (Linear's six); a workspace may rename how a category is shown
// (`TaskSettings.status_names`, carried on a task's detail as `status_names`).
import type { StatusTone } from 'canopy-ui/lib'

import type { Health, Project, Task, TaskStatus } from '@/api/work'

export type Category = 'triage' | 'backlog' | 'unstarted' | 'started' | 'completed' | 'canceled'

export const STATUS_CATEGORY: Record<TaskStatus, Category> = {
  suggested: 'triage',
  backlog: 'backlog',
  todo: 'unstarted',
  in_progress: 'started',
  done: 'completed',
  declined: 'canceled',
}

/** The order a list, a board and a menu show statuses in. */
export const STATUS_ORDER: TaskStatus[] = ['suggested', 'in_progress', 'todo', 'backlog', 'done', 'declined']

/** Open work: what "open" means on every count. */
export const OPEN_STATUSES: TaskStatus[] = ['suggested', 'backlog', 'todo', 'in_progress']

export const DEFAULT_CATEGORY_NAMES: Record<Category, string> = {
  triage: 'Triage',
  backlog: 'Backlog',
  unstarted: 'Todo',
  started: 'In progress',
  completed: 'Done',
  canceled: 'Canceled',
}

export const CATEGORY_TONE: Record<Category, StatusTone> = {
  triage: 'warning',
  backlog: 'neutral',
  unstarted: 'info',
  started: 'primary',
  completed: 'success',
  canceled: 'neutral',
}

export function statusName(status: TaskStatus, names?: Partial<Record<string, string>> | null): string {
  const cat = STATUS_CATEGORY[status] ?? 'triage'
  return names?.[cat] || DEFAULT_CATEGORY_NAMES[cat]
}

export function isOpen(task: Pick<Task, 'status'>): boolean {
  return OPEN_STATUSES.includes(task.status)
}

/** "Waiting" means one thing: waiting on YOU — an open ask, or parked on a person. */
export function asksSomething(task: Pick<Task, 'ask_open' | 'ask_kind'>): boolean {
  return Boolean(task.ask_open && task.ask_kind)
}

export const PRIORITIES = [
  { value: 0, label: 'No priority', short: '—' },
  { value: 1, label: 'Urgent', short: 'Urgent' },
  { value: 2, label: 'High', short: 'High' },
  { value: 3, label: 'Medium', short: 'Med' },
  { value: 4, label: 'Low', short: 'Low' },
] as const

export function priorityLabel(p: number): string {
  return PRIORITIES.find((x) => x.value === p)?.label ?? 'No priority'
}

export const HEALTHS: { value: Health; label: string; tone: StatusTone }[] = [
  { value: 'on_track', label: 'On track', tone: 'success' },
  { value: 'at_risk', label: 'At risk', tone: 'warning' },
  { value: 'off_track', label: 'Off track', tone: 'destructive' },
]

export function healthMeta(h: string | null | undefined): { label: string; tone: StatusTone } | null {
  return HEALTHS.find((x) => x.value === h) ?? null
}

export const PROJECT_STATUSES: { value: Project['status']; label: string; tone: StatusTone }[] = [
  { value: 'proposed', label: 'Proposed', tone: 'warning' },
  { value: 'planned', label: 'Planned', tone: 'info' },
  { value: 'active', label: 'Active', tone: 'primary' },
  { value: 'paused', label: 'Paused', tone: 'neutral' },
  { value: 'done', label: 'Done', tone: 'success' },
  { value: 'canceled', label: 'Canceled', tone: 'neutral' },
  { value: 'archived', label: 'Archived', tone: 'neutral' },
]

export function projectStatusMeta(s: string): { label: string; tone: StatusTone } {
  return PROJECT_STATUSES.find((x) => x.value === s) ?? { label: s, tone: 'neutral' }
}

/** Who a task is with, as one string: an agent's name, a person's email, or "". */
export function assigneeOf(task: Pick<Task, 'agent_slug' | 'assignee_email'>): string {
  if (task.agent_slug) return task.agent_slug
  return task.assignee_email ?? ''
}

/** The value `assignee=` and a PATCH take for an assignee string from `assigneeOf`. */
export function assigneeValue(kind: 'agent' | 'person', id: string): string {
  return kind === 'agent' ? `agent:${id}` : id
}

export function displayAgent(slug: string): string {
  return slug ? slug.charAt(0).toUpperCase() + slug.slice(1) : ''
}

export function displayPerson(email: string): string {
  return email.split('@')[0] ?? email
}

/** Semantic label colours (never palette literals). */
export const LABEL_COLORS = ['muted', 'primary', 'success', 'warning', 'destructive', 'info'] as const

export const LABEL_CLASSES: Record<string, string> = {
  muted: 'bg-muted text-muted-foreground border-border',
  primary: 'bg-primary/10 text-primary border-primary/30',
  success: 'bg-success/10 text-success border-success/30',
  warning: 'bg-warning/10 text-warning border-warning/30',
  destructive: 'bg-destructive/10 text-destructive border-destructive/30',
  info: 'bg-info/10 text-info border-info/30',
}
