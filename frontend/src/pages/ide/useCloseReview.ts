import { listSessions, type ChatSession, type SessionState } from '@/api/chat'
import { describeSelection } from '@/widget/pageState'
import { usePageAction } from '@/widget/usePageAction'
import { usePageState } from '@/widget/usePageState'

/**
 * /ide's half of the close review (spec 2026-10-16-ide-close-review-design.md).
 *
 * The agent in the panel reads the sessions on screen through a page action
 * this tab answers — as YOU, with your own session — rather than a server tool:
 * on your own turn the agent runs on its own login, which reads none of your
 * sessions. Then it proposes with `propose_session_closes`, which shows the
 * review in the panel; only your click there closes anything.
 */

/** At most this many ids ride the page state (its 8 KiB cap) and one read. */
export const REVIEW_MAX = 150
/** The tail of each last reply the agent reads — "done, safe to close" is at the end. */
export const REPLY_TAIL = 1200

export type ReviewCandidate = {
  id: string
  title: string
  project: string
  agent: string
  runner: string
  runner_status: string
  running: boolean
  waiting_on_you: boolean
  mode: string
  last_activity_at: string
  agent_spoke_last: boolean
  last_reply: string
}

export function toCandidate(s: ChatSession): ReviewCandidate {
  const reply = (s.last_reply ?? '').trim()
  return {
    id: s.id,
    title: s.title?.trim() || '(untitled)',
    project: s.emdash_project || s.project || '',
    agent: s.agent_slug ?? '',
    runner: s.runner_name ?? '',
    runner_status: s.runner_status ?? '',
    running: Boolean(s.running),
    waiting_on_you: Boolean(s.waiting_on_you),
    mode: s.turn_mode ?? '',
    last_activity_at: s.last_activity_at,
    agent_spoke_last: Boolean(s.agent_spoke_last),
    last_reply: reply.length > REPLY_TAIL ? `…${reply.slice(-REPLY_TAIL)}` : reply,
  }
}

export function useCloseReview(shown: readonly ChatSession[], state: SessionState): void {
  const ids = shown.slice(0, REVIEW_MAX).map((s) => s.id)

  usePageState(
    () =>
      describeSelection({
        backingTool: 'page_reviewCloseCandidates',
        resource: 'ide.sessions',
        ids,
        extra: shown.length > REVIEW_MAX ? { truncated_from: shown.length } : undefined,
      }),
    [ids.join(','), shown.length],
  )

  usePageAction(
    'reviewCloseCandidates',
    async () => {
      // Read fresh, with replies — the rail's own list carries none.
      const fresh = await listSessions(state, { reply: true, limit: 500 })
      const byId = new Map(fresh.map((s) => [s.id, s]))
      const sessions = ids.flatMap((id) => {
        const s = byId.get(id)
        return s ? [toCandidate(s)] : []
      })
      return {
        sessions,
        note:
          'The sessions on /ide right now, as the rail shows them, with the tail of each last reply ' +
          '(only when the agent had the last word or a dialog is waiting). To suggest closing some, call ' +
          'propose_session_closes with [{session_id, quote, why}] — it shows the person a review; only ' +
          'their click closes. Propose only sessions whose last reply says the work is finished, and ' +
          'never one that is running or waiting on someone.',
      }
    },
    {
      description:
        'Read the sessions the person is looking at on /ide, with each one’s last reply, to judge which ' +
        'say their work is finished. Follow with propose_session_closes.',
      parameters: { type: 'object', properties: {} },
    },
  )
}
