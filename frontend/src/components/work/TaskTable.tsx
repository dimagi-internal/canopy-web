// THE task row — one dense table row, the same everywhere a list of tasks is
// shown (the Tasks page, a project, an agent's tab, Home, the Supervisor), so a
// task looks and behaves the same wherever you meet it. Status, priority,
// assignee and due are edited in place; the title opens the peek (or the
// page); `X` / the checkbox selects for a bulk change.
import { useMemo, type JSX, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { cn } from 'canopy-ui/lib'

import type { Task, TaskPatch } from '@/api/work'
import { useAuth } from '@/auth/AuthProvider'
import { relativeAge } from '@/lib/relativeAge'
import { roleAllows } from '@/lib/workspaceRoles'
import { scopedPath } from '@/lib/scopedLinks'
import { useWorkspaceList } from '@/workspace/WorkspaceProvider'

import { AssigneePicker, DueInput, LabelChips, PriorityPicker, StatusPicker } from './bits'
import {
  STATUS_CATEGORY,
  STATUS_ORDER,
  asksSomething,
  displayAgent,
  displayPerson,
  priorityLabel,
  statusName,
} from './meta'
import type { Directory } from './useDirectory'
import { Td, Th, Tr } from '@/components/DataTable'
import { dueLabel, isOverdue, rhythmLabel } from './recurring'

export type GroupBy = '' | 'status' | 'project' | 'assignee' | 'priority'

export type Column = 'project' | 'assignee' | 'labels' | 'due' | 'updated'

export const ALL_COLUMNS: Column[] = ['project', 'labels', 'assignee', 'due', 'updated']

/** May the signed-in person change a task's fields? An editor of its workspace,
 *  or its assignee. A display hint: the server decides. */
export function useCanEditTask(): (task: Task) => boolean {
  const workspaces = useWorkspaceList()
  const auth = useAuth()
  const me = auth.status === 'authenticated' ? auth.user.email.toLowerCase() : ''
  return useMemo(() => {
    const roles = new Map(workspaces.map((w) => [w.slug, w.role]))
    return (task: Task) =>
      roleAllows(roles.get(task.workspace), 'agent.work') || (!!me && task.assignee_email?.toLowerCase() === me)
  }, [workspaces, me])
}

export interface TaskTableProps {
  tasks: readonly Task[]
  directory: Directory
  onPatch: (key: string, body: TaskPatch, optimistic?: Partial<Task>) => void
  /** Open a task beside the list (the peek). Omitted: the title links to its page. */
  onOpen?: (task: Task) => void
  groupBy?: GroupBy
  columns?: Column[]
  selected?: ReadonlySet<string>
  onToggle?: (key: string, shiftKey: boolean) => void
  /** The row the keyboard is on (J/K). */
  focused?: string | null
  /** Category names as this workspace shows them. */
  names?: Record<string, string> | null
  empty?: ReactNode
  canEdit?: (task: Task) => boolean
  /** Indent subtasks under their parent (a project's list). */
  nest?: boolean
  /** Rows finished here, kept in place (struck through) until the list is reloaded. */
  kept?: ReadonlySet<string>
  /** Put a kept row back as it was: one tap. */
  onUndo?: (task: Task) => void
}

export interface Group {
  key: string
  label: string
  tasks: Task[]
}

/** The groups a table shows, in order — also the order J/K walk. */
export function groupTasks(tasks: readonly Task[], by: GroupBy, names?: Record<string, string> | null): Group[] {
  if (!by) return [{ key: '', label: '', tasks: [...tasks] }]
  const groups = new Map<string, Group>()
  const add = (key: string, label: string, t: Task) => {
    const g = groups.get(key) ?? { key, label, tasks: [] }
    g.tasks.push(t)
    groups.set(key, g)
  }
  for (const t of tasks) {
    if (by === 'status') add(t.status, statusName(t.status, names), t)
    else if (by === 'project') add(t.project_address ?? '', t.project_name ?? 'No project', t)
    else if (by === 'priority') add(String(t.priority ?? 0), priorityLabel(t.priority ?? 0), t)
    else {
      const who = t.agent_slug ? `agent:${t.agent_slug}` : (t.assignee_email ?? '')
      add(who, t.agent_slug ? displayAgent(t.agent_slug) : t.assignee_email ? displayPerson(t.assignee_email) : 'Unassigned', t)
    }
  }
  const list = [...groups.values()]
  if (by === 'status') list.sort((a, b) => STATUS_ORDER.indexOf(a.key as Task['status']) - STATUS_ORDER.indexOf(b.key as Task['status']))
  else if (by === 'priority') list.sort((a, b) => (Number(a.key) || 9) - (Number(b.key) || 9))
  else list.sort((a, b) => (a.key === '' ? 1 : b.key === '' ? -1 : a.label.localeCompare(b.label)))
  return list
}

/** Parents first, each followed by its subtasks (one level of indent per depth). */
function nested(tasks: Task[]): { task: Task; depth: number }[] {
  const byParent = new Map<string, Task[]>()
  const keys = new Set(tasks.map((t) => t.key))
  for (const t of tasks) {
    const p = t.parent_key && keys.has(t.parent_key) ? t.parent_key : ''
    byParent.set(p, [...(byParent.get(p) ?? []), t])
  }
  const out: { task: Task; depth: number }[] = []
  const walk = (parent: string, depth: number) => {
    for (const t of byParent.get(parent) ?? []) {
      out.push({ task: t, depth })
      if (depth < 3) walk(t.key, depth + 1)
    }
  }
  walk('', 0)
  return out
}

export function TaskTable({
  tasks,
  directory,
  onPatch,
  onOpen,
  groupBy = '',
  columns = ALL_COLUMNS,
  selected,
  onToggle,
  focused,
  names,
  empty,
  canEdit,
  nest = false,
  kept,
  onUndo,
}: TaskTableProps): JSX.Element {
  const defaultCanEdit = useCanEditTask()
  const editable = canEdit ?? defaultCanEdit
  const groups = useMemo(() => groupTasks(tasks, groupBy, names), [tasks, groupBy, names])
  const show = new Set(columns)
  if (tasks.length === 0) {
    return <div className="rounded-lg border border-border bg-card px-4 py-6 text-[13px] text-muted-foreground">{empty ?? 'No tasks.'}</div>
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-border bg-card" data-testid="task-table">
      <table className="w-full table-fixed border-collapse text-[13px]">
        <colgroup>
          {/* On a phone a row is its status and its title (with a line under it saying
              when, how often and who): selection, priority, key and assignee columns
              give their width to the title. */}
          {onToggle && <col className="hidden w-7 sm:table-column" />}
          <col className="hidden w-7 sm:table-column" />
          <col className="w-8 sm:w-7" />
          <col className="hidden w-[5.5rem] sm:table-column" />
          <col />
          {show.has('labels') && <col className="hidden w-40 md:table-column" />}
          {show.has('project') && <col className="hidden w-40 lg:table-column" />}
          {show.has('assignee') && <col className="hidden w-32 sm:table-column" />}
          {show.has('due') && <col className="hidden w-[8rem] sm:table-column" />}
          {show.has('updated') && <col className="hidden w-14 md:table-column" />}
        </colgroup>
        {groups.map((g) => (
          <tbody key={g.key || 'all'}>
            {groupBy && (
              <Tr rule="bottom" className="bg-muted/40">
                <Th density="snug" weight="inherit" align="left" colSpan={99} scope="colgroup" className="text-[12px] font-semibold text-foreground">
                  {g.label} <span className="font-normal text-muted-foreground">{g.tasks.length}</span>
                </Th>
              </Tr>
            )}
            {(nest ? nested(g.tasks) : g.tasks.map((task) => ({ task, depth: 0 }))).map(({ task, depth }) => (
              <TaskRow
                key={task.key}
                task={task}
                depth={depth}
                directory={directory}
                onPatch={onPatch}
                onOpen={onOpen}
                show={show}
                selected={selected?.has(task.key) ?? false}
                onToggle={onToggle}
                focused={focused === task.key}
                names={names}
                editable={editable(task)}
                kept={kept?.has(task.key) ?? false}
                onUndo={onUndo}
              />
            ))}
          </tbody>
        ))}
      </table>
    </div>
  )
}

function TaskRow({
  task,
  depth,
  directory,
  onPatch,
  onOpen,
  show,
  selected,
  onToggle,
  focused,
  names,
  editable,
  kept,
  onUndo,
}: {
  task: Task
  depth: number
  directory: Directory
  onPatch: TaskTableProps['onPatch']
  onOpen?: (task: Task) => void
  show: Set<Column>
  selected: boolean
  onToggle?: (key: string, shiftKey: boolean) => void
  focused: boolean
  names?: Record<string, string> | null
  editable: boolean
  kept: boolean
  onUndo?: (task: Task) => void
}): JSX.Element {
  const page = scopedPath(task.workspace, `/tasks/${task.key}`)
  const done = STATUS_CATEGORY[task.status] === 'completed' || STATUS_CATEGORY[task.status] === 'canceled'
  const asking = asksSomething(task)
  const waitingOn = !done && task.waiting_on_email ? { email: task.waiting_on_email, name: displayPerson(task.waiting_on_email) } : null
  return (
    <Tr
      rule="bottom-last"
      data-task={task.key}
      aria-selected={selected}
      className={cn(
        'group hover:bg-muted/50',
        selected && 'bg-primary/5',
        focused && 'outline outline-1 -outline-offset-1 outline-primary/60',
      )}
    >
      {onToggle && (
        <Td density="none" hideBelow="sm" className="pl-2">
          <input
            type="checkbox"
            checked={selected}
            aria-label={`Select ${task.key}`}
            onChange={(e) => onToggle(task.key, (e.nativeEvent as MouseEvent).shiftKey)}
            className={cn('size-3.5 accent-primary', !selected && 'opacity-30 group-hover:opacity-100')}
          />
        </Td>
      )}
      <Td density="narrow" hideBelow="sm">
        <PriorityPicker
          priority={task.priority ?? 0}
          disabled={!editable}
          onChange={(priority) => onPatch(task.key, { priority }, { priority })}
        />
      </Td>
      <Td density="narrow">
        <StatusPicker
          status={task.status}
          names={names}
          disabled={!editable}
          onChange={(status) => onPatch(task.key, { status }, { status })}
        />
      </Td>
      <Td density="narrow" tone="muted" nowrap mono hideBelow="sm" className="text-[11.5px]">{task.key}</Td>
      <Td density="cozy-gutter" className="min-w-0">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 sm:flex-nowrap" style={depth ? { paddingLeft: `${depth * 1.1}rem` } : undefined}>
          {/* The whole title on a phone (it wraps); one dense line from sm up. */}
          {onOpen ? (
            <button
              type="button"
              onClick={() => onOpen(task)}
              className={cn('min-w-0 break-words text-left font-medium hover:text-primary sm:truncate', done ? 'text-muted-foreground line-through' : 'text-foreground')}
            >
              {task.title}
            </button>
          ) : (
            <Link to={page} className={cn('min-w-0 break-words font-medium hover:text-primary sm:truncate', done ? 'text-muted-foreground line-through' : 'text-foreground')}>
              {task.title}
            </Link>
          )}
          {kept && onUndo && (
            <button
              type="button"
              onClick={() => onUndo(task)}
              data-testid={`undo-${task.key}`}
              className="shrink-0 rounded border border-border px-1.5 text-[11.5px] text-primary hover:bg-muted"
            >
              Undo
            </button>
          )}
          <RecurringChips task={task} className="hidden sm:inline-flex" />
          {task.recurrence && (
            <span className="hidden shrink-0 text-[11px] text-muted-foreground sm:inline">
              {task.last_done ? `last done ${relativeAge(task.last_done, new Date(), { seconds: false, words: true })}` : 'not done yet'}
            </span>
          )}
          {asking && (
            <span className="shrink-0 rounded border border-warning/30 bg-warning/10 px-1 text-[10.5px] font-medium text-warning">
              {task.ask_kind === 'question' ? 'Question' : 'Decide'}
            </span>
          )}
          {waitingOn && (
            <span
              className="shrink-0 rounded border border-warning/30 bg-warning/10 px-1 text-[10.5px] text-warning"
              title={`Waiting on ${waitingOn.email}`}
              data-testid={`waiting-${task.key}`}
            >
              Waiting on {waitingOn.name}
            </span>
          )}
          {task.kind === 'idea' && (
            <span className="shrink-0 rounded border border-special/30 bg-special/10 px-1 text-[10.5px] text-special">Idea</span>
          )}
          {/* Its one-line brief — what happens next — so a reader need not open it. */}
          {task.next_action && (
            <span className="hidden min-w-0 flex-1 basis-0 truncate text-[12px] text-muted-foreground sm:inline" title={task.next_action} data-testid={`brief-${task.key}`}>
              {task.next_action}
            </span>
          )}
        </div>
        <PhoneLine task={task} done={done} />
      </Td>
      {show.has('labels') && (
        <Td density="cozy-gutter" hideBelow="md">
          <LabelChips labels={task.labels ?? []} max={2} />
        </Td>
      )}
      {show.has('project') && (
        <Td density="cozy-gutter" tone="secondary" hideBelow="lg" className="truncate text-[12px]">
          {task.project_handle ? (
            <Link to={scopedPath(task.project_address?.split('/')[0] ?? task.workspace, `/projects/${task.project_handle}`)} className="hover:text-primary">
              {task.project_name}
            </Link>
          ) : (
            <span className="text-foreground-subtle">—</span>
          )}
        </Td>
      )}
      {show.has('assignee') && (
        <Td density="cozy-gutter" hideBelow="sm" className="truncate text-[12px]">
          <AssigneePicker
            task={task}
            directory={directory}
            disabled={!editable}
            onChange={(assignee) =>
              onPatch(task.key, { assignee }, assignee.startsWith('agent:')
                ? { agent_slug: assignee.slice(6), assignee_email: null }
                : { agent_slug: null, assignee_email: assignee || null })
            }
          />
        </Td>
      )}
      {show.has('due') && (
        <Td density="none" hideBelow="sm" className="py-1.5 pr-1">
          <DueInput value={task.due} disabled={!editable} onChange={(due) => onPatch(task.key, { due }, { due })} />
        </Td>
      )}
      {show.has('updated') && (
        <Td density="cozy-gap" tone="muted" nowrap align="right" hideBelow="md" className="text-[11.5px]" title={task.updated_at}>
          {relativeAge(task.updated_at, new Date(), { seconds: false })}
        </Td>
      )}
    </Tr>
  )
}

/** A template's rhythm ("Every Wed") and an instance's template ("copy of MAR-10"). */
export function RecurringChips({ task, className }: { task: Task; className?: string }): JSX.Element | null {
  if (task.recurrence) {
    return (
      <span
        className={cn('shrink-0 items-center rounded border border-info/30 bg-info/10 px-1 text-[10.5px] text-info', className)}
        data-testid={`rhythm-${task.key}`}
      >
        {rhythmLabel(task.recurrence)}
      </span>
    )
  }
  if (task.template_key) {
    return (
      <span
        className={cn('shrink-0 items-center text-[11px] text-muted-foreground', className)}
        title={task.template_title ? `A copy of ${task.template_key} ${task.template_title}` : undefined}
        data-testid={`copy-of-${task.key}`}
      >
        copy of {task.template_key}
      </span>
    )
  }
  return null
}

/** A phone row's second line: when it is due, how it repeats, who has it (only when someone
 *  does), and — de-emphasized — its key. Hidden from sm up, where the columns say it. */
function PhoneLine({ task, done }: { task: Task; done: boolean }): JSX.Element {
  const late = !done && task.due && isOverdue(task.due)
  const who = task.agent_slug ? displayAgent(task.agent_slug) : task.assignee_email ? displayPerson(task.assignee_email) : ''
  return (
    <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11.5px] text-muted-foreground sm:hidden" data-testid={`phone-line-${task.key}`}>
      {task.due && <span className={late ? 'text-destructive' : undefined}>{dueLabel(task.due)}</span>}
      <RecurringChips task={task} className="inline-flex" />
      {task.recurrence && task.last_done && (
        <span>last done {relativeAge(task.last_done, new Date(), { seconds: false, words: true })}</span>
      )}
      {who && <span>{who}</span>}
      <span className="font-mono text-[10.5px] text-foreground-subtle">{task.key}</span>
    </div>
  )
}
