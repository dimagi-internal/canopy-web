import { useCallback, useEffect, useState } from 'react'
import type { UseSessionSocketResult } from 'canopy-ui/chat'

import { getSession, requestBackfill, type ChatSessionDetail } from '@/api/chat'
import { backfillAction, restToKitMessage } from '../chatPageLogic'

/**
 * "Load full session" WAITS for the rows, it does not guess at them.
 *
 * It used to sleep a flat 1200 ms and read once. Measured on labs (2026-07-31)
 * the rows landed at t+14.6s, so the read happened 13 s early, returned the
 * identical tail, and the button reported success having changed nothing —
 * which is exactly what it looks like from a phone. Now it polls until the
 * transcript actually grows, which is also the only formulation that stays
 * correct as the runner gets faster: with eager persistence most sessions are
 * already complete server-side and the first poll returns immediately.
 */
export const BACKFILL_POLL_MS = 600
export const BACKFILL_TIMEOUT_MS = 30_000

/** `historyError` when the server says no reachable runner is bound. */
export const RUNNER_OFFLINE = 'runner offline'

/** The whole server transcript ("Load all messages") and the full backfill
 *  from the runner ("Load full session"). Whether the server holds more than
 *  the tail is seeded from the REST detail via `seed` — the WS `session.state`
 *  snapshot doesn't carry it, it's frozen to the tail page it was built from. */
export function useTranscriptPaging(
  sessionId: string,
  prependMessages: UseSessionSocketResult['prependMessages'],
) {
  const [hasMoreBefore, setHasMoreBefore] = useState(false)
  const [loadingAll, setLoadingAll] = useState(false)
  const [loadingFull, setLoadingFull] = useState(false)
  // Why the full history isn't on screen, or null when nothing went wrong. Only
  // the server's `unavailable` answer means the runner is unreachable; a refused
  // or failed request is reported as itself. Every failure used to read
  // "runner offline", which is false on a running session (2026-10-30: a 500
  // on a live jj-mbp-cdp session showed it under a green "running").
  const [historyError, setHistoryError] = useState<string | null>(null)

  useEffect(() => {
    setHistoryError(null)
  }, [sessionId])

  const seed = useCallback((m: Pick<ChatSessionDetail, 'has_more_before'>) => {
    setHasMoreBefore(m.has_more_before)
  }, [])

  // "Load all messages", not one page back: whoever reaches for this menu item
  // wants the whole conversation, and paging back by hand meant tapping it over
  // and over. One full read of what the server holds — the runner backfill is
  // still "Load full session".
  const loadAll = useCallback(async () => {
    if (loadingAll) return
    // Capture the session this call was made for. Belt-and-suspenders on top
    // of the route-level `key={id}` remount (router.tsx): even if this
    // component instance somehow outlives the session it was fetching for,
    // a stale response can never splice into whatever session is current.
    const requestedId = sessionId
    setLoadingAll(true)
    try {
      const full = await getSession(requestedId, { full: true })
      if (requestedId !== sessionId) return
      if (full.messages.length > 0) prependMessages(full.messages.map(restToKitMessage))
      setHasMoreBefore(false)
    } catch {
      /* keep what's shown; the item stays available to retry */
    } finally {
      if (requestedId === sessionId) setLoadingAll(false)
    }
  }, [sessionId, loadingAll, prependMessages])

  const loadFull = useCallback(async () => {
    if (loadingFull) return
    // See loadAll: capture the session this call was made for so a
    // stale response can never apply to a different session.
    const requestedId = sessionId
    setHistoryError(null)
    setLoadingFull(true)
    try {
      const res = await requestBackfill(requestedId)
      if (requestedId !== sessionId) return
      const action = backfillAction(res.status)
      if (action === 'unavailable') {
        setHistoryError(RUNNER_OFFLINE)
        return
      }
      if (action === 'reload-after-delay') {
        // The runner is shipping. Poll the TAIL read for `backfill_pending` —
        // it carries the same flag for a nineteenth of the bytes (measured on
        // labs: 43 KB / 0.36s against 825 KB / 1.6-2.8s for ?full=true on a
        // 940-row session). Polling the full read instead meant ~11 downloads
        // of the entire transcript, on a phone, to answer a yes/no question;
        // it was slow enough to time out a 90s client. The flag is the exact
        // signal (cleared by the runner's final chunk), so waiting on it beats
        // both a fixed sleep and watching the row count grow — growth cannot
        // tell "still arriving" from "there was nothing more to send".
        const deadline = Date.now() + BACKFILL_TIMEOUT_MS
        while (Date.now() < deadline) {
          await new Promise((r) => setTimeout(r, BACKFILL_POLL_MS))
          if (requestedId !== sessionId) return
          const probe = await getSession(requestedId)
          if (requestedId !== sessionId) return
          if (!probe.backfill_pending) break
        }
      }
      // Exactly ONE full read, after the wait — not one per poll. Whatever has
      // landed by the deadline is applied either way: a partially rebuilt
      // history beats discarding it and showing the tail.
      const full = await getSession(requestedId, { full: true })
      if (requestedId !== sessionId) return
      prependMessages(full.messages.map(restToKitMessage))
      setHasMoreBefore(false)
    } catch (err) {
      if (requestedId === sessionId) {
        setHistoryError(err instanceof Error && err.message ? err.message : 'the request failed')
      }
    } finally {
      if (requestedId === sessionId) setLoadingFull(false)
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
