import { useEffect, useRef } from 'react'
import { tailDivergence, type SessionState, type UseSessionSocketResult } from 'canopy-ui/chat'

import { getSession } from '@/api/chat'
import { recordEvents } from '@/api/logs'
import { restToKitMessage } from '../chatPageLogic'

/** How long after a turn ends to look: its last rows (your line among them) can
 *  trail its end by a report or two. */
export const SETTLE_DELAY_MS = 5_000
/** A tab hidden this long has likely slept through a reconnect. */
export const AWAY_MS = 60_000
/** A difference must outlast this before it counts: a row the server holds may
 *  still be on its way to the page as a live frame. */
export const RECHECK_MS = 3_000

/**
 * Checks the open page against the server's rows, and puts it right.
 *
 * After each turn ends, and when you come back to a tab that has been hidden a
 * while, the page reads the server's tail (`GET /{id}`, what a reload shows)
 * and compares it with what it holds (`tailDivergence`). On a difference it
 * takes the server's rows (`resyncTail`) and records a `chat.diverged` event,
 * ids only, so the cause can be found. A failed read changes nothing.
 */
export function useTailCheck(
  sessionId: string,
  state: SessionState,
  resyncTail: UseSessionSocketResult['resyncTail'],
) {
  const messagesRef = useRef(state.messages)
  messagesRef.current = state.messages

  useEffect(() => {
    let alive = true
    let timer: ReturnType<typeof setTimeout> | undefined

    const check = (why: 'turn-ended' | 'returned') => {
      getSession(sessionId)
        .then((server) => {
          if (!alive) return
          const rows = server.messages.map(restToKitMessage)
          if (!tailDivergence(messagesRef.current, rows)) return
          return new Promise<void>((resolve) => setTimeout(resolve, RECHECK_MS)).then(() => {
            if (!alive) return
            const diff = tailDivergence(messagesRef.current, rows)
            if (!diff) return
            resyncTail(rows, {
              hasMoreBefore: server.has_more_before,
              oldestLoadedTurnIndex: server.oldest_loaded_turn_index ?? null,
            })
            recordEvents([
              {
                source: 'web.chat',
                kind: 'chat.diverged',
                level: 'warn',
                key: sessionId,
                summary:
                  `open chat differed from the server (${why}): ${diff.missing.length} missing, ` +
                  `${diff.extra.length} extra${diff.reordered ? ', out of order' : ''}`,
                payload: { session: sessionId, why, ...diff },
              },
            ]).catch(() => {})
          })
        })
        .catch(() => {})
    }

    const finished = state.turn_status?.settled ? state.turn_status.finished_at : null
    if (finished) timer = setTimeout(() => check('turn-ended'), SETTLE_DELAY_MS)

    let hiddenAt: number | null = null
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') {
        hiddenAt = Date.now()
      } else if (hiddenAt != null && Date.now() - hiddenAt >= AWAY_MS) {
        hiddenAt = null
        timer = setTimeout(() => check('returned'), SETTLE_DELAY_MS)
      }
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      alive = false
      if (timer) clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [sessionId, state.turn_status?.settled, state.turn_status?.finished_at, resyncTail])
}
