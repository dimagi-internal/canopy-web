import type { AgentTurnOut } from '@/api/agents'

// One Turn row is both halves: what was DISPATCHED (prompt, origin, status,
// result_note) and, if the agent closed the turn out, what it REPORTED (title,
// summary). Most turns never get a report — a scheduled turn that ran and found
// nothing to do, an api turn whose session ended without `canopy agent turn` —
// so every display string here prefers the report and falls back to the
// dispatch half rather than rendering blank.

const HEADLINE_MAX = 140

/** The card's heading: the reported title, else the first line of the prompt
 * (markdown heading/list markers stripped), else what triggered the turn. */
export function turnHeadline(turn: AgentTurnOut): string {
  if (turn.title.trim()) return turn.title.trim()
  const line = (turn.prompt ?? '')
    .split('\n')
    .map((l) => l.replace(/^\s*(#{1,6}|[-*>]|\d+\.)\s+/, '').trim())
    .find((l) => l.length > 0)
  if (line) return line.length > HEADLINE_MAX ? `${line.slice(0, HEADLINE_MAX - 1)}…` : line
  return `${turnTrigger(turn)} turn`
}

/** The card's body text: the reported summary, else the runner's result note. */
export function turnBody(turn: AgentTurnOut): string {
  const text = turn.summary.trim() || (turn.result_note ?? '').trim()
  // A turn's prompt and result are a log: below admin you read only the turns
  // you started (apps/harness/turn_access.py). Say so rather than show nothing.
  if (!text && turn.content_hidden) return 'Details are visible to whoever started this turn, the agent’s admins, and workspace admins.'
  return text
}

/** What fired the turn, in the words the schedule/routing UI uses. */
export function turnTrigger(turn: AgentTurnOut): string {
  const ref = (turn.origin_ref ?? {}) as Record<string, unknown>
  if (turn.origin === 'canopy_scheduler') {
    // The schedule's NAME, not its slot: the slot is the fire time as an ISO
    // timestamp, which the card already shows as its date.
    if (typeof ref.schedule_name === 'string' && ref.schedule_name) return `schedule · ${ref.schedule_name}`
    if (ref.manual) return 'schedule · run now'
    return 'schedule'
  }
  return turn.origin || 'turn'
}

/** Wall time from start to finish, or '' when either end is missing. */
export function turnDuration(turn: AgentTurnOut): string {
  if (!turn.started_at || !turn.ended_at) return ''
  const secs = Math.max(
    0,
    Math.round((new Date(turn.ended_at).getTime() - new Date(turn.started_at).getTime()) / 1000),
  )
  if (secs < 60) return `${secs}s`
  const mins = Math.round(secs / 60)
  if (mins < 60) return `${mins}m`
  return `${Math.floor(mins / 60)}h ${mins % 60}m`
}

/** True when the prompt says more than the headline already shows. */
export function promptHasMore(turn: AgentTurnOut): boolean {
  const prompt = (turn.prompt ?? '').trim()
  return prompt.length > 0 && prompt !== turnHeadline(turn)
}
