import type { LocalAppRun } from '@/api/localApps'

/** A heartbeat's `state` and reasons are short identifiers from the app, such as `stopped_from_website`. */
function words(identifier: string): string {
  return identifier.replace(/[_-]+/g, ' ')
}

function countsText(counts: LocalAppRun['counts']): string {
  return Object.entries(counts)
    .map(([name, n]) => `${words(name)} ${String(n)}`)
    .join(', ')
}

/** A person's reading of an installation's last run. */
export function describeRun(run: LocalAppRun | null | undefined): string {
  if (!run) return 'Not run yet'
  const counts = countsText(run.counts)
  const tail = counts ? ` (${counts})` : ''
  if (run.running) {
    if (run.stop_requested) return `Stopping${tail}`
    return `Running${run.state ? `: ${words(run.state)}` : ''}${tail}`
  }
  // No end was reported and no heartbeat is arriving: a computer that slept or went offline.
  if (!run.finished_at) return 'Stopped reporting'
  return `Ended${run.stop_reason ? `: ${words(run.stop_reason)}` : ''}${tail}`
}
