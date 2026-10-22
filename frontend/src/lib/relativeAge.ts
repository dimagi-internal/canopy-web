/**
 * "How long ago" (or "how long until") — the app's one relative-time formatter.
 *
 * The default is the runner list's reading: second granularity under a minute,
 * because a heartbeat's whole job is to be seconds fresh and "just now" cannot
 * distinguish a runner that reported 2s ago from one that reported 59s ago.
 *
 * The ladder runs all the way up (s → m → h → d → w → mo → y), so the unit is
 * always one a person reads at a glance — never `drilled 1087h ago` (45 days).
 *
 * Options cover the other real readings:
 * - `seconds: false` — "just now" under a minute, for lists (turns, sessions).
 * - `future: true` — a time ahead reads "in 5m" (a schedule's next run, an
 *   expiry). Without it the age is past-only and anything over a minute ahead
 *   is an anomaly that says "in the future".
 * - `dateAfterDays: n` — from n days on, the date ("Oct 7") instead of an age.
 * - `words: true` — plain words ("3 min ago", "yesterday", "2 days ago") for
 *   pages written for people who never see the compact form (/people/me).
 */
export interface RelativeAgeOptions {
  seconds?: boolean
  future?: boolean
  dateAfterDays?: number
  words?: boolean
}

// [compact, word singular, word plural]
const UNITS = {
  s: ['s', 'sec', 'sec'],
  m: ['m', 'min', 'min'],
  h: ['h', 'h', 'h'],
  d: ['d', 'day', 'days'],
  w: ['w', 'week', 'weeks'],
  mo: ['mo', 'month', 'months'],
  y: ['y', 'year', 'years'],
} as const

export function relativeAge(
  iso: string | null | undefined,
  now: Date = new Date(),
  opts: RelativeAgeOptions = {},
): string {
  const { seconds = true, future = false, dateAfterDays, words = false } = opts
  if (!iso) return 'never'
  const ms = new Date(iso).getTime()
  if (Number.isNaN(ms)) return 'never'

  let secs = Math.round((now.getTime() - ms) / 1000)
  // A clock skewed a little ahead of the server should read as fresh, not as a
  // negative age. Anything beyond a minute in the future is a real anomaly and
  // says so rather than pretending — unless the caller asked for future times.
  if (!future) {
    if (secs < -60) return 'in the future'
    secs = Math.max(secs, 0)
  }
  const ahead = secs < 0
  const abs = Math.abs(secs)

  const say = (n: number, unit: keyof typeof UNITS): string => {
    const [compact, one, many] = UNITS[unit]
    const amount = words ? `${n} ${n === 1 ? one : many}` : `${n}${compact}`
    return ahead ? `in ${amount}` : `${amount} ago`
  }

  if (abs < 60) {
    if (seconds) return say(abs, 's')
    return ahead ? 'in a moment' : 'just now'
  }

  const mins = Math.round(abs / 60)
  if (mins < 60) return say(mins, 'm')

  const hours = Math.round(mins / 60)
  if (hours < 24) return say(hours, 'h')

  const days = Math.round(hours / 24)
  if (dateAfterDays !== undefined && days >= dateAfterDays) {
    return new Date(ms).toLocaleDateString([], { month: 'short', day: 'numeric' })
  }
  if (words && days === 1) return ahead ? 'tomorrow' : 'yesterday'
  if (days < 7) return say(days, 'd')

  const weeks = Math.round(days / 7)
  if (weeks < 5) return say(weeks, 'w')

  const months = Math.round(days / 30)
  if (months < 12) return say(months, 'mo')

  return say(Math.round(days / 365), 'y')
}
