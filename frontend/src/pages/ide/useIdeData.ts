import { useCallback, useEffect, useRef, useState } from 'react'

import { listMyAgents, type AgentOut } from '@/api/agents'
import { listSessions, type ChatSession } from '@/api/chat'
import { rememberListed } from '@/pages/chat/sessionCache'
import { listRunners, type RunnerOut } from '@/api/harness'
import { settleClosing } from '@/components/chat/closeAction'
import { usePolled } from '@/hooks/usePolled'
import { useReconnectingSocket } from '@/hooks/useReconnectingSocket'
import { wsUrl } from '@/lib/wsUrl'

export const POLL_MS = 15_000
// The list endpoint's ceiling (no paging). Asked for in full so archived
// sessions are not silently cut at the default 200; hitting it is said aloud.
export const LIST_LIMIT = 500
// A runner reports every ~10s per box, and each report rings the doorbell; one
// refetch per window is plenty.
export const DOORBELL_MIN_MS = 3_000
export const UNCONFIRMED_DELETE = 'A delete was not confirmed by its runner — try again, or delete it in emdash.'

export type IdeNote = { text: string; error: boolean }

/** What /ide shows: every session (active, or all with archived), the runners,
 *  your agents (for "New session"), and a clock for the row ages.
 *
 *  Kept fresh three ways: a 15s poll (paused while the tab is hidden), a
 *  refetch on focus, and the ws/supervisor/ doorbell — every runner report
 *  rings it, so a session changing state shows within seconds; the poll is the
 *  fallback. Deletes relayed to a runner are tracked in `closing` until its
 *  report retires the row (see settleClosing). */
export function useIdeData(state: 'all' | 'active') {
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [runners, setRunners] = useState<RunnerOut[]>([])
  const [agents, setAgents] = useState<AgentOut[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [now, setNow] = useState(() => new Date())
  const [note, setNote] = useState<IdeNote | null>(null)
  // Closes relayed to a runner, by session id → when asked. The row stays,
  // marked, until the runner's report retires it (see settleClosing).
  const [closing, setClosing] = useState<Record<string, number>>({})
  const closingRef = useRef(closing)
  useEffect(() => {
    closingRef.current = closing
  }, [closing])
  // Deletes the list has called unconfirmed, still watched: one that lands
  // after all takes its error back rather than leaving it standing.
  const unconfirmed = useRef<Set<string>>(new Set())
  const lastFetch = useRef(0)
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null)

  const refresh = useCallback(() => {
    lastFetch.current = Date.now()
    return Promise.allSettled([listSessions(state, { limit: LIST_LIMIT }), listRunners()]).then(([s, r]) => {
      if (s.status === 'fulfilled') {
        // Which rows are private, so a click opens an ordinary chat without the
        // gate's read first (PrivateConversationGate).
        rememberListed(s.value)
        setSessions(s.value)
        setError(null)
        if (Object.keys(closingRef.current).length) {
          const settled = settleClosing(closingRef.current, s.value, Date.now())
          setClosing(settled.pending)
          if (settled.stuck.length || settled.failed.length) {
            for (const sid of [...settled.stuck, ...settled.failed]) unconfirmed.current.add(sid)
            setNote({ text: UNCONFIRMED_DELETE, error: true })
          }
        }
        if (unconfirmed.current.size) {
          const active = new Set(s.value.filter((x) => x.status === 'active').map((x) => x.id))
          if ([...unconfirmed.current].every((sid) => !active.has(sid))) {
            unconfirmed.current.clear()
            setNote((n) => (n?.text === UNCONFIRMED_DELETE ? { text: 'Deleted', error: false } : n))
          }
        }
      } else {
        setError(s.reason instanceof Error ? s.reason.message : 'failed to load sessions')
      }
      if (r.status === 'fulfilled') setRunners(r.value ?? [])
      setNow(new Date())
      setLoading(false)
    })
  }, [state])

  usePolled(refresh, { ms: POLL_MS, deps: [refresh], refreshOnFocus: true })

  // The agents "New session" offers; repos come from the runners' reports.
  useEffect(() => {
    let live = true
    listMyAgents({ limit: 100 })
      .then((page) => { if (live) setAgents(page.items) })
      .catch(() => { /* repos alone still make a usable menu */ })
    return () => {
      live = false
    }
  }, [])

  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), 30_000)
    return () => window.clearInterval(t)
  }, [])

  // At most one doorbell refetch per DOORBELL_MIN_MS since the last fetch.
  const ring = useCallback(() => {
    if (pending.current) return
    const wait = Math.max(0, lastFetch.current + DOORBELL_MIN_MS - Date.now())
    pending.current = setTimeout(() => {
      pending.current = null
      void refresh()
    }, wait)
  }, [refresh])
  useEffect(() => () => {
    if (pending.current) clearTimeout(pending.current)
  }, [])
  const { connected: live } = useReconnectingSocket(wsUrl('ws/supervisor/'), ring, { onBadFrame: ring })

  return {
    sessions,
    runners,
    agents,
    loading,
    error,
    now,
    note,
    setNote,
    closing,
    setClosing,
    refresh,
    live,
  }
}
