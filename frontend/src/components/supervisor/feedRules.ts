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
