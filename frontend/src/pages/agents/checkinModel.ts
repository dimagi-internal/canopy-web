import type { CheckinRun } from '@/api/checkins'

/** Plain words for check-in runs (apps/huddles/checkins.py), shared by the project page and the huddle page. */

export const RUN_STATUS: Record<string, string> = {
  awaiting_confirm: 'waiting for the lead',
  reading: 'reading the channel',
  running: 'working',
  done: 'done',
  skipped: 'skipped',
  expired: 'lapsed',
  failed: 'failed',
}

function slackTime(ts: string): string {
  if (!ts) return ''
  return new Date(parseFloat(ts) * 1000).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  })
}

function day(iso: string | null | undefined): string {
  return iso ? new Date(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : ''
}

/** "no channel" · "read 12 messages since Oct 13 (Oct 14 → Oct 16)" · "nothing read". */
export function readWindow(r: Pick<CheckinRun, 'status' | 'channel_id' | 'read_messages' | 'read_first_ts' | 'read_last_ts' | 'read_since'>): string {
  if (!r.channel_id) return 'no channel'
  if (r.status === 'awaiting_confirm' || r.status === 'skipped' || r.status === 'expired') return 'nothing read'
  if (r.status === 'reading') return 'reading…'
  const since = r.read_since ? ` since ${day(r.read_since)}` : ''
  if (!r.read_messages) return r.status === 'failed' ? 'nothing read' : `nothing new${since}`
  const span = r.read_first_ts ? ` (${slackTime(r.read_first_ts)} → ${slackTime(r.read_last_ts)})` : ''
  return `${r.read_messages} message${r.read_messages === 1 ? '' : 's'}${since}${span}`
}
