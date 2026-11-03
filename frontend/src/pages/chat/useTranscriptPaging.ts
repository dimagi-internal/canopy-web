import { useCallback, useEffect, useState } from 'react'
import type { UseSessionSocketResult } from 'canopy-ui/chat'

import { getSession, requestBackfill, type ChatSessionDetail } from '@/api/chat'
import { backfillAction, restToKitMessage } from '../chatPageLogic'

/**
 * "Load full session" WAITS for the rows, it does not guess at them: it polls
 * the tail read's `backfill_pending` until the runner's last chunk has landed
 * (or BACKFILL_TIMEOUT_MS passes), then reads the whole transcript once. With
 * eager persistence most sessions are already complete server-side and the
 * first poll returns immediately.
 */
export const BACKFILL_POLL_MS = 600
export const BACKFILL_TIMEOUT_MS = 30_000

/** `historyError` when the server says no reachable runner is bound. */
export const RUNNER_OFFLINE = 'runner offline'

/** The whole server transcript ("Load all messages") and the full backfill
 *  from the runner ("Load full session").
 *
 *  Whether the server holds more than what is loaded: the socket state's
 *  `has_more_before` (`socketHasMore`) when it has one — the snapshot sends it
 *  and the reducer keeps it with the rows across reconnects — else the REST
 *  detail's, seeded via `seed`. A load hands the answer to the socket state
 *  with its rows, so both agree.
 *
 *  The page is remounted per session (`key={id}`), so nothing here has to
 *  guard against a response landing in another session. */
export function useTranscriptPaging(
  sessionId: string,
  prependMessages: UseSessionSocketResult['prependMessages'],
  socketHasMore?: boolean,
) {
  const [seededHasMore, setHasMoreBefore] = useState(false)
  const hasMoreBefore = socketHasMore ?? seededHasMore
  const [loadingAll, setLoadingAll] = useState(false)
  const [loadingFull, setLoadingFull] = useState(false)
  // Why the full history isn't on screen, or null when nothing went wrong. Only
  // the server's `unavailable` answer means the runner is unreachable; a refused
  // or failed request is reported as itself.
  const [historyError, setHistoryError] = useState<string | null>(null)

  useEffect(() => {
    setHistoryError(null)
  }, [sessionId])

  const seed = useCallback((m: Pick<ChatSessionDetail, 'has_more_before'>) => {
    setHasMoreBefore(m.has_more_before)
  }, [])

  // "Load all messages", not one page back: whoever reaches for it wants the
  // whole conversation. One full read of what the server holds — the runner
  // backfill is "Load full session".
  const loadAll = useCallback(async () => {
    if (loadingAll) return
    setLoadingAll(true)
    try {
      const full = await getSession(sessionId, { full: true })
      prependMessages(full.messages.map(restToKitMessage), { hasMoreBefore: false })
      setHasMoreBefore(false)
    } catch {
      /* keep what's shown; the item stays available to retry */
    } finally {
      setLoadingAll(false)
    }
  }, [sessionId, loadingAll, prependMessages])

  const loadFull = useCallback(async () => {
    if (loadingFull) return
    setHistoryError(null)
    setLoadingFull(true)
    try {
      const res = await requestBackfill(sessionId)
      const action = backfillAction(res.status)
      if (action === 'unavailable') {
        setHistoryError(RUNNER_OFFLINE)
        return
      }
      if (action === 'reload-after-delay') {
        // The runner is shipping. Poll the TAIL read for `backfill_pending`: it
        // carries the same flag as the full read for a small fraction of the
        // bytes, and the runner's final chunk clears it — an exact signal,
        // where watching the row count cannot tell "still arriving" from
        // "there was nothing more to send".
        const deadline = Date.now() + BACKFILL_TIMEOUT_MS
        while (Date.now() < deadline) {
          await new Promise((r) => setTimeout(r, BACKFILL_POLL_MS))
          const probe = await getSession(sessionId)
          if (!probe.backfill_pending) break
        }
      }
      // Exactly ONE full read, after the wait. Whatever has landed by the
      // deadline is applied: a partly rebuilt history beats the tail alone.
      const full = await getSession(sessionId, { full: true })
      prependMessages(full.messages.map(restToKitMessage), { hasMoreBefore: false })
      setHasMoreBefore(false)
    } catch (err) {
      setHistoryError(err instanceof Error && err.message ? err.message : 'the request failed')
    } finally {
      setLoadingFull(false)
    }
  }, [sessionId, loadingFull, prependMessages])

  return {
    hasMoreBefore,
    loadingAll,
    loadingFull,
    historyUnavailable: historyError !== null,
    historyError,
    seed,
    loadAll,
    loadFull,
  }
}
