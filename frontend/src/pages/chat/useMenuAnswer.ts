import { useCallback, useEffect, useState } from 'react'
import type { UseSessionSocketResult } from 'canopy-ui/chat'

import { answerMenu } from '@/api/chat'
import { ANSWER_GRACE_MS, answerHidesMenu, menuIdentity, type PendingAnswer } from '../chatPageLogic'

type SocketState = UseSessionSocketResult['state']

/** Answering the dialog a blocked agent is waiting on.
 *
 *  The clear is optimistic and happens at TAP time, not when the server
 *  confirms: nothing retracts the menu until the next session report (~10s) or
 *  the agent's next row, and until then a phone showed live buttons, "needs
 *  you" and a locked composer over an answer that had already landed. The
 *  runner re-reads the screen and refuses a stale answer, so the worst case is
 *  a menu that comes back (see `answerHidesMenu`), which is better than a tap
 *  that looks ignored. */
export function useMenuAnswer(
  sessionId: string,
  menu: SocketState['menu'],
  activity: SocketState['activity'],
) {
  const [answering, setAnswering] = useState(false)
  const [answerError, setAnswerError] = useState('')
  const [pendingAnswer, setPendingAnswer] = useState<PendingAnswer | null>(null)
  const [answerClock, setAnswerClock] = useState(0)

  const answer = useCallback(
    async (option: number | null, selections?: number[][] | null,
           texts?: (string | null)[] | null) => {
      if (!sessionId) return
      setAnswering(true)
      setAnswerError('')
      if (menu) {
        setPendingAnswer({
          key: menuIdentity(menu),
          note: menu.answer_note ?? '',
          activity,
          at: Date.now(),
        })
      }
      try {
        const res = await answerMenu(sessionId, option, selections, texts)
        if (!res.ok) {
          // Not relayed, so nothing will press the key. Put the dialog back
          // with the reason instead of claiming the answer was sent.
          setPendingAnswer(null)
          setAnswerError(
            res.reason === 'unavailable'
              ? 'That runner is offline — answer it in emdash.'
              : res.reason === 'unbound'
                ? 'This session has no runner to answer on.'
                : 'Could not answer.',
          )
        }
      } catch {
        setPendingAnswer(null)
        setAnswerError('Could not answer.')
      } finally {
        setAnswering(false)
      }
    },
    [sessionId, menu, activity],
  )

  // Wake up when the grace window runs out, so a dialog that is STILL reported
  // comes back without waiting for some unrelated frame to trigger a render.
  useEffect(() => {
    if (!pendingAnswer) return
    const t = setTimeout(() => setAnswerClock((n) => n + 1),
      Math.max(0, pendingAnswer.at + ANSWER_GRACE_MS - Date.now()) + 50)
    return () => clearTimeout(t)
  }, [pendingAnswer])

  const menuHidden = Boolean(menu && answerHidesMenu(menu, pendingAnswer, Date.now()))
  const visibleMenu = menuHidden ? undefined : menu

  // Let go of the answer once the truth has caught up: the agent's activity has
  // moved on (it is working, or idle again), or a dialog is showing that this
  // answer does not cover (refused, a different dialog, or grace expired).
  useEffect(() => {
    if (!pendingAnswer) return
    if (activity !== pendingAnswer.activity && activity !== 'blocked') {
      setPendingAnswer(null)
    } else if (menu && !menuHidden) {
      setPendingAnswer(null)
    }
  }, [pendingAnswer, activity, menu, menuHidden, answerClock])

  return { answering, answerError, pendingAnswer, menuHidden, visibleMenu, answer }
}
