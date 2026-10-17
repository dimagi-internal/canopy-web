import type { HuddleGroupRun } from '@/api/huddleGroups'

/** Plain words for work groups (apps/huddles/groups.py), shared by the list and the group page. */

export function isAdmin(role: string | null): boolean {
  return role === 'owner' || role === 'admin'
}

function slackTime(ts: string): string {
  if (!ts) return ''
  return new Date(parseFloat(ts) * 1000).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  })
}

export function readWindow(r: HuddleGroupRun): string {
  if (r.status === 'awaiting_confirm' || r.status === 'skipped' || r.status === 'expired') return 'nothing read'
  if (r.status === 'reading') return 'reading…'
  if (!r.read_messages) return r.status === 'failed' ? 'nothing read' : 'nothing new'
  const span = r.read_first_ts ? ` (${slackTime(r.read_first_ts)} → ${slackTime(r.read_last_ts)})` : ''
  return `read ${r.read_messages} message${r.read_messages === 1 ? '' : 's'}${span}`
}

