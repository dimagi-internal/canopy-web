import type { ChatSession } from '@/api/chat'
import { partitionByRunnerReachability } from '@/components/chat/runnerEligibility'

/**
 * Is this session waiting for the person's next prompt?
 *
 * Two ways to be: blocked on a dialog (`waiting_on_you`), or finished — the
 * agent had the last word and is no longer working. A RUNNING session whose
 * last row is the agent's is mid-turn, not done; it joins the feed when the
 * turn ends.
 */
export function needsNextPrompt(s: ChatSession): boolean {
  return Boolean(s.waiting_on_you) || (Boolean(s.agent_spoke_last) && !s.running)
}

/**
 * The supervisor feed: sessions waiting on the person, newest first, with
 * blocked-on-a-dialog ahead of merely finished (a dialog holds the agent
 * mid-turn; a finished one can wait).
 *
 * Sessions whose runner is paused or offline are held back and COUNTED, not
 * shown: a reply to one queues until that box returns, so it does not belong
 * in a list of things you can move forward right now — but the feed says how
 * many it is withholding rather than quietly dropping them.
 */
export function feedSessions(sessions: readonly ChatSession[]): {
  feed: ChatSession[]
  parked: number
} {
  const waiting = sessions.filter(needsNextPrompt)
  const { live, parked } = partitionByRunnerReachability(waiting)
  const feed = [...live].sort(
    (a, b) =>
      Number(Boolean(b.waiting_on_you)) - Number(Boolean(a.waiting_on_you)) ||
      Date.parse(b.last_activity_at) - Date.parse(a.last_activity_at),
  )
  return { feed, parked: parked.length }
}

// The feed is per PERSON, across every workspace they are in — so it has to stay
// usable as the fleet grows. These thresholds keep the density tools out of the
// way while the feed is short (one person, a handful of agents), and bring them
// in once it is long enough that scanning needs help.
/** Filter chips appear at this many cards (and 2+ sources). */
export const CHIPS_AT = 4
/** Cards open as a short preview above this many. */
export const COMPACT_ABOVE = 5

/** Which agent — or, for an agentless chat, which project — a session is. */
export function sourceKey(s: ChatSession): string {
  return s.agent_slug ? `agent:${s.agent_slug}` : `project:${s.project || ''}`
}

/** One chip per source in the feed, busiest first; ties keep feed order. */
export function feedSources(feed: readonly ChatSession[]): { key: string; count: number; sample: ChatSession }[] {
  const by = new Map<string, { key: string; count: number; sample: ChatSession }>()
  for (const s of feed) {
    const k = sourceKey(s)
    const hit = by.get(k)
    if (hit) hit.count += 1
    else by.set(k, { key: k, count: 1, sample: s })
  }
  return [...by.values()].sort((a, b) => b.count - a.count)
}

/**
 * Did an agent drive this session on its own — its newest turn ran in `auto`
 * mode, and came from somewhere other than a person's chat (a schedule, an
 * email, Slack, a dispatch)? The feed holds these back unless asked: an auto
 * turn already acted without waiting for anyone, so it is not asking for your
 * next prompt the way a conversation is. A chat you are having stays, whatever
 * the agent's switch says.
 */
export function ranOnItsOwn(s: ChatSession): boolean {
  return s.turn_mode === 'auto' && s.turn_origin !== 'canopy_web_chat'
}
