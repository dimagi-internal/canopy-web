// How a recurring task reads in a list: a template's rhythm ("Every Wed",
// "Monthly", "As needed"), an instance's template ("copy of MAR-10"), and a due
// date as a person says it ("Wed" this week, "Nov 19" later).
import type { components } from '@/api/generated'
import type { Task } from '@/api/work'

export type Recurrence = components['schemas']['Recurrence']

const DAY_SHORT: Record<string, string> = {
  mon: 'Mon', tue: 'Tue', wed: 'Wed', thu: 'Thu', fri: 'Fri', sat: 'Sat', sun: 'Sun',
}
const DAY_LONG: Record<string, string> = {
  mon: 'Monday', tue: 'Tuesday', wed: 'Wednesday', thu: 'Thursday', fri: 'Friday', sat: 'Saturday', sun: 'Sunday',
}

/** The rhythm as a chip ("Every Wed") or, `long`, in a sentence ("every Wednesday"). */
export function rhythmLabel(rec: Recurrence, long = false): string {
  const day = rec.weekday ? (long ? DAY_LONG : DAY_SHORT)[rec.weekday] : ''
  switch (rec.every) {
    case 'day':
      return long ? 'every day' : 'Daily'
    case 'weekdays':
      return long ? 'every weekday' : 'Weekdays'
    case 'week':
      return day ? `${long ? 'every' : 'Every'} ${day}` : long ? 'every week' : 'Weekly'
    case '2weeks':
      return day ? `${long ? 'every other' : 'Every other'} ${day}` : long ? 'every two weeks' : 'Every 2 weeks'
    case 'month':
      return long ? 'every month' : 'Monthly'
    case 'as_needed':
      return long ? 'as needed' : 'As needed'
  }
}

export function isTemplate(task: Pick<Task, 'recurrence'>): boolean {
  return task.recurrence != null
}

function parseDay(iso: string): Date {
  const [y, m, d] = iso.split('-').map(Number)
  return new Date(y, (m ?? 1) - 1, d ?? 1)
}

/** "Today", "Tomorrow", a weekday within the coming week, else "Nov 19" (with the year when not this one). */
export function dueLabel(due: string, today: Date = new Date()): string {
  const day = parseDay(due)
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate())
  const diff = Math.round((day.getTime() - start.getTime()) / 86_400_000)
  if (diff === 0) return 'Today'
  if (diff === 1) return 'Tomorrow'
  if (diff > 1 && diff < 7) return day.toLocaleDateString('en-US', { weekday: 'short' })
  return day.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    ...(day.getFullYear() !== today.getFullYear() ? { year: 'numeric' } : {}),
  })
}

/** Past due, and not done. */
export function isOverdue(due: string, today: Date = new Date()): boolean {
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate())
  return parseDay(due).getTime() < start.getTime()
}
