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

export type GroupBy = '' | 'status' | 'project' | 'assignee' | 'priority'

export type Column = 'project' | 'assignee' | 'labels' | 'due' | 'updated'

const ALL_COLUMNS: Column[] = ['project', 'labels', 'assignee', 'due', 'updated']

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
          {onToggle && <col className="w-7" />}
          <col className="w-7" />
          <col className="w-7" />
          <col className="w-[5.5rem]" />
          <col />
          {show.has('labels') && <col className="hidden w-40 md:table-column" />}
          {show.has('project') && <col className="hidden w-40 lg:table-column" />}
          {show.has('assignee') && <col className="w-32" />}
          {show.has('due') && <col className="hidden w-[8rem] sm:table-column" />}
          {show.has('updated') && <col className="hidden w-14 md:table-column" />}
        </colgroup>
        {groups.map((g) => (
          <tbody key={g.key || 'all'}>
            {groupBy && (
              <tr className="border-b border-border bg-muted/40">
                <th colSpan={99} scope="colgroup" className="px-3 py-1.5 text-left text-[12px] font-semibold text-foreground">
                  {g.label} <span className="font-normal text-muted-foreground">{g.tasks.length}</span>
                </th>
              </tr>
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
}): JSX.Element {
  const page = scopedPath(task.workspace, `/tasks/${task.key}`)
  const done = STATUS_CATEGORY[task.status] === 'completed' || STATUS_CATEGORY[task.status] === 'canceled'
  const asking = asksSomething(task)
  return (
    <tr
      data-task={task.key}
      aria-selected={selected}
      className={cn(
        'group border-b border-border last:border-0 hover:bg-muted/50',
        selected && 'bg-primary/5',
        focused && 'outline outline-1 -outline-offset-1 outline-primary/60',
      )}
    >
      {onToggle && (
        <td className="pl-2">
          <input
            type="checkbox"
            checked={selected}
            aria-label={`Select ${task.key}`}
            onChange={(e) => onToggle(task.key, (e.nativeEvent as MouseEvent).shiftKey)}
            className={cn('size-3.5 accent-primary', !selected && 'opacity-30 group-hover:opacity-100')}
          />
        </td>
      )}
      <td className="px-1 py-1.5">
        <PriorityPicker
          priority={task.priority ?? 0}
          disabled={!editable}
          onChange={(priority) => onPatch(task.key, { priority }, { priority })}
        />
      </td>
      <td className="px-1 py-1.5">
        <StatusPicker
          status={task.status}
          names={names}
          disabled={!editable}
          onChange={(status) => onPatch(task.key, { status }, { status })}
        />
      </td>
      <td className="whitespace-nowrap px-1 py-1.5 font-mono text-[11.5px] text-muted-foreground">{task.key}</td>
      <td className="min-w-0 py-1.5 pr-2">
        <div className="flex min-w-0 items-center gap-2" style={depth ? { paddingLeft: `${depth * 1.1}rem` } : undefined}>
          {onOpen ? (
            <button
              type="button"
              onClick={() => onOpen(task)}
              className={cn('truncate text-left font-medium hover:text-primary', done ? 'text-muted-foreground line-through' : 'text-foreground')}
            >
              {task.title}
            </button>
          ) : (
            <Link to={page} className={cn('truncate font-medium hover:text-primary', done ? 'text-muted-foreground line-through' : 'text-foreground')}>
              {task.title}
            </Link>
          )}
          {asking && (
            <span className="shrink-0 rounded border border-warning/30 bg-warning/10 px-1 text-[10.5px] font-medium text-warning">
              {task.ask_kind === 'question' ? 'Question' : 'Decide'}
            </span>
          )}
          {task.kind === 'idea' && (
            <span className="shrink-0 rounded border border-special/30 bg-special/10 px-1 text-[10.5px] text-special">Idea</span>
          )}
        </div>
      </td>
      {show.has('labels') && (
        <td className="hidden py-1.5 pr-2 md:table-cell">
          <LabelChips labels={task.labels ?? []} max={2} />
        </td>
      )}
      {show.has('project') && (
        <td className="hidden truncate py-1.5 pr-2 text-[12px] text-foreground-secondary lg:table-cell">
          {task.project_handle ? (
            <Link to={scopedPath(task.project_address?.split('/')[0] ?? task.workspace, `/projects/${task.project_handle}`)} className="hover:text-primary">
              {task.project_name}
            </Link>
          ) : (
            <span className="text-foreground-subtle">—</span>
          )}
        </td>
      )}
      {show.has('assignee') && (
        <td className="truncate py-1.5 pr-2 text-[12px]">
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
        </td>
      )}
      {show.has('due') && (
        <td className="hidden py-1.5 pr-1 sm:table-cell">
          <DueInput value={task.due} disabled={!editable} onChange={(due) => onPatch(task.key, { due }, { due })} />
        </td>
      )}
      {show.has('updated') && (
        <td className="hidden whitespace-nowrap py-1.5 pr-3 text-right text-[11.5px] text-muted-foreground md:table-cell" title={task.updated_at}>
          {relativeAge(task.updated_at, new Date(), { seconds: false })}
        </td>
      )}
    </tr>
  )
}
