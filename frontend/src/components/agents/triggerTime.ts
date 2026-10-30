// What a trigger's schedule fields need: the presets, and two wall-clock helpers.

export const PRESETS: { label: string; cron: string }[] = [
  { label: 'Weekly — Friday 9am', cron: '0 9 * * 5' },
  { label: 'Weekly — Monday 9am', cron: '0 9 * * 1' },
  { label: 'Monthly — 1st, 9am', cron: '0 9 1 * *' },
  { label: 'Daily — 9am', cron: '0 9 * * *' },
]

/** `iso` as wall-clock "YYYY-MM-DDTHH:MM" in `tz` — the value a
 * datetime-local input holds. The server reads an offset-less run_once_at in
 * the schedule's timezone, so the client never does zone arithmetic. */
export function wallClock(iso: string, tz: string): string {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-CA', {
      timeZone: tz,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    })
      .formatToParts(new Date(iso))
      .map((p) => [p.type, p.value]),
  )
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`
}

export function tomorrowNineAm(): string {
  const d = new Date()
  d.setDate(d.getDate() + 1)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T09:00`
}
