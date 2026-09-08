/**
 * "How long ago", for the runner list.
 *
 * Deliberately NOT `activity/turnLog`'s `relativeTime`, which floors anything
 * under a minute to "just now": a heartbeat's whole job is to be seconds fresh,
 * and "just now" cannot distinguish a runner that reported 2s ago from one that
 * reported 59s ago.
 *
 * It stopped at hours, which is how the Runners tab came to report a readiness
 * drill as `drilled 1087h ago`. Nobody converts 1087 hours; it is 45 days. The
 * ladder now runs all the way up, so the unit is always one a person reads at a
 * glance.
 */
export function relativeAge(iso: string | null, now: Date = new Date()): string {
  if (!iso) return 'never'
  const ms = new Date(iso).getTime()
  if (Number.isNaN(ms)) return 'never'

  const secs = Math.round((now.getTime() - ms) / 1000)
  // A clock skewed a little ahead of the server should read as fresh, not as a
  // negative age. Anything beyond a minute in the future is a real anomaly and
  // says so rather than pretending.
  if (secs < -60) return 'in the future'
  if (secs < 60) return `${Math.max(secs, 0)}s ago`

  const mins = Math.round(secs / 60)
  if (mins < 60) return `${mins}m ago`

  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours}h ago`

  const days = Math.round(hours / 24)
  if (days < 7) return `${days}d ago`

  const weeks = Math.round(days / 7)
  if (weeks < 5) return `${weeks}w ago`

  const months = Math.round(days / 30)
  if (months < 12) return `${months}mo ago`

  return `${Math.round(days / 365)}y ago`
}
