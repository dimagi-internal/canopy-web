import type { ChatSession } from '@/api/chat'

/**
 * The supervisor feed: sessions waiting on the person, OLDEST first — a queue.
 * Newest-first meant the top kept refilling and the bottom of the feed was
 * never reached, so the least recently touched session waited longest. Now the
 * one that has waited longest is at the top and new arrivals join the bottom.
 * Blocked-on-a-dialog gets no jump: a session that becomes blocked would leap
 * to the top and break the queue; the card's "needs an answer" badge marks it.
 *
 * WHICH sessions is not decided here. The server stamps each row with its
 * `feed_status` for the caller (`apps/canopy_sessions/feed.py`), and the same
 * rule decides who is pushed about a session — so the feed and the phone cannot
 * disagree. This only sorts and counts:
 *   - `waiting` is on the feed;
 *   - `auto` (an agent's own run) is held back unless `showAuto`, and counted;
 *   - `parked` (paused/offline runner) is held back and COUNTED, not shown —
 *     a reply would queue until that box returns;
 *   - `not_yours` (someone else's runner) is not yours to answer, so it is
 *     neither shown nor counted: its owner has it on their own feed.
 */
export function feedSessions(
  sessions: readonly ChatSession[],
  { showAuto = false }: { showAuto?: boolean } = {},
): {
  feed: ChatSession[]
  parked: number
  auto: number
} {
  const shown = sessions.filter(
    (s) => s.feed_status === 'waiting' || (showAuto && s.feed_status === 'auto'),
  )
  const feed = [...shown].sort(
    (a, b) => Date.parse(a.last_activity_at) - Date.parse(b.last_activity_at),
  )
  return {
    feed,
    parked: sessions.filter((s) => s.feed_status === 'parked').length,
    auto: sessions.filter((s) => s.feed_status === 'auto').length,
  }
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

/** The runner fields the parked banner reads — structural, so a test can pass a literal. */
export type ParkedRunnerInfo = {
  id: string
  name: string
  status_note?: string | null
  ready_note?: string | null
  paused_note?: string | null
}

/** One runner the feed is holding sessions back for. */
export type ParkedGroup = {
  /** The runner's name as the session reports it ('' when it reported none). */
  runnerName: string
  /** Its fleet row, when the viewer's fleet list has it — that is what links to it. */
  runner: ParkedRunnerInfo | null
  /** A pause is somebody's decision; offline/degraded is a box to fix. */
  reason: 'paused' | 'offline'
  count: number
  /** Of those, blocked on a dialog. */
  waiting: number
  /** Why the runner cannot take turns, in its own words ('' when it said nothing). */
  note: string
}

/**
 * The parked sessions grouped by the runner holding them, busiest first.
 *
 * A bare count ("+ 7 more waiting on a paused or offline runner") reads fine
 * when one box of many is down, and wrong when the box that is down is the
 * viewer's own laptop: on 2026-10-04 every one of a person's sessions dropped
 * off the feed because emdash had been relaunched without CDP, and the only
 * clue was that link. Naming the runner and its own note — `paused_note` for a
 * pause, else `status_note`, else `ready_note` (where "emdash CDP unreachable"
 * lands) — says what to go and fix.
 */
export function parkedByRunner(
  sessions: readonly ChatSession[],
  runners: readonly ParkedRunnerInfo[] | null | undefined,
): ParkedGroup[] {
  const fleet = new Map((runners ?? []).map((r) => [r.name, r] as const))
  const by = new Map<string, ParkedGroup>()
  for (const s of sessions) {
    if (s.feed_status !== 'parked') continue
    const name = s.runner_name ?? ''
    const hit = by.get(name)
    if (hit) {
      hit.count += 1
      if (s.waiting_on_you) hit.waiting += 1
      continue
    }
    const reason: ParkedGroup['reason'] = s.runner_status === 'paused' ? 'paused' : 'offline'
    const runner = fleet.get(name) ?? null
    const note = runner
      ? (reason === 'paused' ? runner.paused_note : '') || runner.status_note || runner.ready_note || ''
      : ''
    by.set(name, {
      runnerName: name, runner, reason, count: 1, waiting: s.waiting_on_you ? 1 : 0, note: note.trim(),
    })
  }
  return [...by.values()].sort((a, b) => b.count - a.count)
}
