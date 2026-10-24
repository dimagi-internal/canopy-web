// The calendar: a month of weeks (Monday first) with each task on its due day,
// and the tasks with no due date listed beside it — the list's same rows, laid
// out by when they are due. The month is the caller's (it lives in the URL).
import { useMemo, type JSX } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { cn } from 'canopy-ui/lib'

import type { Task } from '@/api/work'

import { StatusIcon } from './bits'
import { STATUS_CATEGORY } from './meta'
import { isoDay } from './viewState'

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

/** `YYYY-MM` → that month's first day; anything else → this month's. */
export function monthStart(month: string, today: Date = new Date()): Date {
  const m = /^(\d{4})-(\d{2})$/.exec(month)
  return m ? new Date(Number(m[1]), Number(m[2]) - 1, 1) : new Date(today.getFullYear(), today.getMonth(), 1)
}

export function shiftMonth(first: Date, by: number): string {
  const d = new Date(first.getFullYear(), first.getMonth() + by, 1)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
}

/** The month's grid: whole weeks, Monday to Sunday, padded with the neighbours' days. */
export function monthGrid(first: Date): Date[] {
  const lead = (first.getDay() + 6) % 7
  const start = new Date(first.getFullYear(), first.getMonth(), 1 - lead)
  const last = new Date(first.getFullYear(), first.getMonth() + 1, 0)
  const cells = lead + last.getDate()
  const total = Math.ceil(cells / 7) * 7
  return Array.from({ length: total }, (_, i) => new Date(start.getFullYear(), start.getMonth(), start.getDate() + i))
}

export function TaskCalendar({
  tasks,
  month,
  onMonth,
  onOpen,
  focused,
}: {
  tasks: readonly Task[]
  month: string
  onMonth: (month: string) => void
  onOpen: (task: Task) => void
  focused?: string | null
}): JSX.Element {
  const first = monthStart(month)
  const days = monthGrid(first)
  const today = isoDay(new Date())
  const byDay = useMemo(() => {
    const m = new Map<string, Task[]>()
    for (const t of tasks) if (t.due) m.set(t.due, [...(m.get(t.due) ?? []), t])
    return m
  }, [tasks])
  const undated = tasks.filter((t) => !t.due)
  const title = first.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })

  return (
    <div className="flex flex-col gap-3 lg:flex-row" data-testid="task-calendar">
      <div className="min-w-0 flex-1 rounded-lg border border-border bg-card">
        <div className="flex items-center gap-2 border-b border-border px-3 py-1.5">
          <h3 className="text-[13px] font-semibold text-foreground">{title}</h3>
          <div className="ml-auto flex items-center gap-1">
            <button type="button" aria-label="Previous month" onClick={() => onMonth(shiftMonth(first, -1))} className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground">
              <ChevronLeft className="size-4" aria-hidden="true" />
            </button>
            <button type="button" onClick={() => onMonth('')} className="rounded px-1.5 py-0.5 text-[12px] text-muted-foreground hover:bg-muted hover:text-foreground">
              Today
            </button>
            <button type="button" aria-label="Next month" onClick={() => onMonth(shiftMonth(first, 1))} className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground">
              <ChevronRight className="size-4" aria-hidden="true" />
            </button>
          </div>
        </div>
        <div className="grid grid-cols-7 border-b border-border text-[11px] text-muted-foreground">
          {WEEKDAYS.map((d) => (
            <div key={d} className="px-1.5 py-1">
              {d}
            </div>
          ))}
        </div>
        <div className="grid grid-cols-7">
          {days.map((d) => {
            const iso = isoDay(d)
            const inMonth = d.getMonth() === first.getMonth()
            const due = byDay.get(iso) ?? []
            return (
              <div
                key={iso}
                data-date={iso}
                className={cn('min-h-20 border-b border-r border-border p-1 [&:nth-child(7n)]:border-r-0', !inMonth && 'bg-muted/30')}
              >
                <div className={cn('mb-0.5 text-[11px]', iso === today ? 'font-semibold text-primary' : inMonth ? 'text-foreground-secondary' : 'text-foreground-subtle')}>
                  {d.getDate()}
                </div>
                <div className="flex flex-col gap-0.5">
                  {due.map((t) => (
                    <CalendarTask key={t.key} task={t} onOpen={onOpen} focused={focused === t.key} />
                  ))}
                </div>
              </div>
            )
          })}
        </div>
      </div>
      <aside className="w-full shrink-0 rounded-lg border border-border bg-card lg:w-60" aria-label="No due date" data-testid="undated">
        <h3 className="border-b border-border px-3 py-1.5 text-[12px] font-semibold text-foreground">
          No due date <span className="font-normal text-muted-foreground">{undated.length}</span>
        </h3>
        <div className="flex max-h-[32rem] flex-col gap-0.5 overflow-y-auto p-1">
          {undated.length === 0 ? (
            <p className="px-2 py-1 text-[12px] text-muted-foreground">Everything here has a date.</p>
          ) : (
            undated.map((t) => <CalendarTask key={t.key} task={t} onOpen={onOpen} focused={focused === t.key} />)
          )}
        </div>
      </aside>
    </div>
  )
}

function CalendarTask({ task, onOpen, focused }: { task: Task; onOpen: (t: Task) => void; focused: boolean }): JSX.Element {
  const done = ['completed', 'canceled'].includes(STATUS_CATEGORY[task.status])
  return (
    <button
      type="button"
      data-task={task.key}
      onClick={() => onOpen(task)}
      title={`${task.key} ${task.title}`}
      className={cn(
        'flex min-w-0 items-center gap-1 rounded px-1 py-0.5 text-left text-[11.5px] hover:bg-muted',
        focused && 'outline outline-1 -outline-offset-1 outline-primary/60',
        done ? 'text-muted-foreground line-through' : 'text-foreground',
      )}
    >
      <StatusIcon status={task.status} className="size-3" />
      <span className="truncate">{task.title}</span>
    </button>
  )
}
