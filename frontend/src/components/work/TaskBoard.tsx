// The board: one column per status (in STATUS_ORDER), a dense card per task.
// Drag a card to another column to change its status, or within a column to
// reorder it — a drop sends `before` (the card just ABOVE where it lands) and
// `after` (the one just below), which is how the server places it by rank.
import { useState, type DragEvent, type JSX } from 'react'
import { cn } from 'canopy-ui/lib'

import type { Task, TaskPatch, TaskStatus } from '@/api/work'

import { Assignee, LabelChips, PriorityIcon, StatusIcon } from './bits'
import { STATUS_ORDER, statusName } from './meta'

/** Where a card lands: the neighbours a PATCH names. `column` is the target
 *  column's keys as shown (it may include the dragged card), `index` the slot
 *  among the OTHER cards (0 = top). */
export function placement(column: readonly string[], dragged: string, index: number): { before?: string; after?: string } {
  const others = column.filter((k) => k !== dragged)
  const i = Math.max(0, Math.min(others.length, index))
  const out: { before?: string; after?: string } = {}
  if (i > 0) out.before = others[i - 1]
  if (i < others.length) out.after = others[i]
  return out
}

export interface TaskBoardProps {
  tasks: readonly Task[]
  names?: Record<string, string> | null
  onOpen: (task: Task) => void
  /** A drop: the status it landed in and its new neighbours. */
  onMove: (key: string, status: TaskStatus, place: { before?: string; after?: string }) => void
  selected?: ReadonlySet<string>
  onToggle?: (key: string, shiftKey: boolean) => void
  focused?: string | null
  canEdit?: (task: Task) => boolean
  /** Statuses to show even when empty (default: all of them). */
  statuses?: readonly TaskStatus[]
}

export function TaskBoard({
  tasks,
  names,
  onOpen,
  onMove,
  selected,
  onToggle,
  focused,
  canEdit,
  statuses = STATUS_ORDER,
}: TaskBoardProps): JSX.Element {
  const [dragging, setDragging] = useState<string | null>(null)
  const [dropAt, setDropAt] = useState<{ status: TaskStatus; index: number } | null>(null)
  const columns = statuses.map((s) => ({ status: s, tasks: tasks.filter((t) => t.status === s) }))

  const over = (e: DragEvent, status: TaskStatus, index: number) => {
    if (!dragging) return
    e.preventDefault()
    e.stopPropagation()
    if (dropAt?.status !== status || dropAt.index !== index) setDropAt({ status, index })
  }
  const drop = (e: DragEvent, status: TaskStatus, keys: string[]) => {
    e.preventDefault()
    e.stopPropagation()
    const key = dragging ?? e.dataTransfer?.getData('text/plain')
    const index = dropAt?.status === status ? dropAt.index : keys.filter((k) => k !== key).length
    setDragging(null)
    setDropAt(null)
    if (key) onMove(key, status, placement(keys, key, index))
  }

  return (
    <div className="flex gap-2 overflow-x-auto pb-2" data-testid="task-board">
      {columns.map(({ status, tasks: col }) => {
        const keys = col.map((t) => t.key)
        const others = keys.filter((k) => k !== dragging)
        return (
          <section
            key={status}
            aria-label={statusName(status, names)}
            data-column={status}
            onDragOver={(e) => over(e, status, others.length)}
            onDrop={(e) => drop(e, status, keys)}
            className={cn(
              'flex w-64 shrink-0 flex-col rounded-lg border border-border bg-muted/30',
              dropAt?.status === status && 'border-primary/50',
            )}
          >
            <h3 className="flex items-center gap-1.5 px-2.5 py-2 text-[12px] font-semibold text-foreground">
              <StatusIcon status={status} />
              {statusName(status, names)}
              <span className="font-normal text-muted-foreground">{col.length}</span>
            </h3>
            <div className="flex min-h-12 flex-col gap-1 px-1.5 pb-1.5">
              {col.map((t) => {
                const slot = others.indexOf(t.key)
                const editable = canEdit ? canEdit(t) : true
                return (
                  <div key={t.key}>
                    {dropAt?.status === status && slot >= 0 && dropAt.index === slot && <DropLine />}
                    <Card
                      task={t}
                      editable={editable}
                      selected={selected?.has(t.key) ?? false}
                      focused={focused === t.key}
                      dragging={dragging === t.key}
                      onOpen={onOpen}
                      onToggle={onToggle}
                      onDragStart={(e) => {
                        setDragging(t.key)
                        e.dataTransfer?.setData('text/plain', t.key)
                      }}
                      onDragEnd={() => {
                        setDragging(null)
                        setDropAt(null)
                      }}
                      onDragOver={(e) => {
                        if (slot < 0) return over(e, status, others.length)
                        const r = e.currentTarget.getBoundingClientRect()
                        over(e, status, e.clientY < r.top + r.height / 2 ? slot : slot + 1)
                      }}
                    />
                  </div>
                )
              })}
              {dropAt?.status === status && dropAt.index >= others.length && <DropLine />}
            </div>
          </section>
        )
      })}
    </div>
  )
}

function DropLine(): JSX.Element {
  return <div aria-hidden="true" className="my-0.5 h-0.5 rounded bg-primary" />
}

function Card({
  task,
  editable,
  selected,
  focused,
  dragging,
  onOpen,
  onToggle,
  onDragStart,
  onDragEnd,
  onDragOver,
}: {
  task: Task
  editable: boolean
  selected: boolean
  focused: boolean
  dragging: boolean
  onOpen: (task: Task) => void
  onToggle?: (key: string, shiftKey: boolean) => void
  onDragStart: (e: DragEvent<HTMLDivElement>) => void
  onDragEnd: () => void
  onDragOver: (e: DragEvent<HTMLDivElement>) => void
}): JSX.Element {
  return (
    <div
      data-task={task.key}
      draggable={editable}
      aria-selected={selected}
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      onDragOver={onDragOver}
      className={cn(
        'group rounded-md border border-border bg-card px-2 py-1.5 text-[12.5px] shadow-xs',
        editable && 'cursor-grab',
        selected && 'border-primary/40 bg-primary/5',
        focused && 'outline outline-1 -outline-offset-1 outline-primary/60',
        dragging && 'opacity-50',
      )}
    >
      <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        {onToggle && (
          <input
            type="checkbox"
            checked={selected}
            aria-label={`Select ${task.key}`}
            onChange={(e) => onToggle(task.key, (e.nativeEvent as MouseEvent).shiftKey)}
            className={cn('size-3 accent-primary', !selected && 'opacity-30 group-hover:opacity-100')}
          />
        )}
        <PriorityIcon priority={task.priority ?? 0} className="size-3" />
        <span className="font-mono">{task.key}</span>
        <span className="ml-auto min-w-0">
          <Assignee task={task} compact />
        </span>
      </div>
      <button
        type="button"
        onClick={() => onOpen(task)}
        className="mt-0.5 line-clamp-2 text-left font-medium text-foreground hover:text-primary"
      >
        {task.title}
      </button>
      {(task.labels?.length ?? 0) > 0 && (
        <div className="mt-1">
          <LabelChips labels={task.labels ?? []} max={3} />
        </div>
      )}
    </div>
  )
}

/** The PATCH a drop sends: the new status (when it changed) and the neighbours. */
export function movePatch(task: Pick<Task, 'status'>, status: TaskStatus, place: { before?: string; after?: string }): TaskPatch | null {
  const body: { status?: string; before?: string; after?: string } = {}
  if (status !== task.status) body.status = status
  if (place.before) body.before = place.before
  if (place.after) body.after = place.after
  return Object.keys(body).length ? (body as TaskPatch) : null
}
