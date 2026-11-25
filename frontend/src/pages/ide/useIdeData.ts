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
// While the doorbell socket is open the poll is only a fallback for a frame lost.
export const LIVE_POLL_MS = 60_000
// The fleet list is the costliest read canopy-web serves: re-read it on a poll at
// most this often, and at once only when a runner's status changes.
export const RUNNERS_MAX_AGE_MS = 60_000
// The list endpoint's ceiling (no paging). Asked for in full so archived
// sessions are not silently cut at the default 200; hitting it is said aloud.
export const LIST_LIMIT = 500
// A frame that changes something refetches at most once per window.
export const DOORBELL_MIN_MS = 5_000
export const UNCONFIRMED_DELETE = 'A delete was not confirmed by its runner — try again, or delete it in emdash.'

/** A status line. `closing` ties it to a delete relayed to a runner: the line
 *  is replaced with `doneText` when the row leaves the listing, or with
 *  UNCONFIRMED_DELETE when the runner never confirms — it never stands. */
export type IdeNote = { text: string; error: boolean; closing?: { id: string; doneText: string } }

/** What a ws/supervisor/ frame changes for /ide. Every runner heartbeat sends a
 *  `supervisor.runner` frame and every session report a `supervisor.sessions`
 *  one, changed or not; only a new status, or a session list that differs from
 *  the last one pushed, is a change. `seen` is the caller's memory of both. */
export type FrameChange = 'none' | 'sessions' | 'all'
export interface FrameMemo { status: Map<string, string>; sessions: string | null }
export function frameChange(raw: unknown, seen: FrameMemo): FrameChange {
  const frame = (raw ?? {}) as { type?: unknown; runner?: { id?: unknown; status?: unknown }; runners?: unknown; sessions?: unknown }
  switch (frame.type) {
    case 'supervisor.runner': {
      const id = String(frame.runner?.id ?? '')
      const status = String(frame.runner?.status ?? '')
      const before = seen.status.get(id)
      seen.status.set(id, status)
      return before === status ? 'none' : 'all'
    }
    case 'supervisor.sessions': {
      const key = JSON.stringify(frame.sessions ?? null)
      if (key === seen.sessions) return 'none'
      seen.sessions = key
      return 'sessions'
    }
    case 'supervisor.snapshot': {
      // A (re)connect: frames may have been missed while it was down.
      seen.status.clear()
      for (const r of Array.isArray(frame.runners) ? frame.runners : []) {
        const row = r as { id?: unknown; status?: unknown }
        seen.status.set(String(row.id ?? ''), String(row.status ?? ''))
      }
      return 'all'
    }
    case 'supervisor.waiting':
      return 'none'
    default:
      return 'all'
  }
}

/** What /ide shows: every session (active, or all with archived), the runners,
 *  your agents (for "New session"), and a clock for the row ages.
 *
 *  Kept fresh three ways: the ws/supervisor/ doorbell — a frame that changes
 *  something (`frameChange`) refetches, so a session changing state shows
 *  within seconds; a poll (paused while the tab is hidden) every 15s without the
 *  socket and every 60s with it; and a refetch on focus. The runner list is
 *  re-read at most every 60s unless a runner's status changed. Deletes relayed to a runner are tracked in `closing` until its
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

  const lastRunners = useRef(0)
  const wantRunners = useRef(false)

  // `runners`: 'always', 'stale' (only when the last read is older than
  // RUNNERS_MAX_AGE_MS) or 'never'.
  const load = useCallback((runners: 'always' | 'stale' | 'never') => {
    const now = Date.now()
    lastFetch.current = now
    const fleet = runners === 'always' || (runners === 'stale' && now - lastRunners.current >= RUNNERS_MAX_AGE_MS)
    if (fleet) lastRunners.current = now
    return Promise.allSettled([listSessions(state, { limit: LIST_LIMIT }), fleet ? listRunners() : Promise.resolve(null)]).then(([s, r]) => {
      if (s.status === 'fulfilled') {
        // Which rows are private, so a click opens an ordinary chat without the
        // gate's read first (PrivateConversationGate).
        rememberListed(s.value)
        setSessions(s.value)
        setError(null)
        if (Object.keys(closingRef.current).length) {
          const settled = settleClosing(closingRef.current, s.value, Date.now())
          setClosing(settled.pending)
          if (settled.done.length) {
            const done = new Set(settled.done)
            setNote((n) => (n?.closing && done.has(n.closing.id) ? { text: n.closing.doneText, error: false } : n))
          }
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
      if (r.status === 'fulfilled' && r.value !== null) setRunners(r.value)
      setNow(new Date())
      setLoading(false)
    })
  }, [state])
  const refresh = useCallback(() => load('always'), [load])
  const poll = useCallback(() => load('stale'), [load])

  /** A delete the runner has been asked to carry out: mark the row, say so,
   *  and let the next listing that no longer has it say it is done. */
  const trackClose = useCallback((sessionId: string, pendingText: string, doneText: string) => {
    const since = Date.now()
    closingRef.current = { ...closingRef.current, [sessionId]: since }
    setClosing((prev) => ({ ...prev, [sessionId]: since }))
    setNote({ text: pendingText, error: false, closing: { id: sessionId, doneText } })
  }, [])

  const liveRef = useRef(false)
  usePolled(poll, { ms: () => (liveRef.current ? LIVE_POLL_MS : POLL_MS), deps: [poll], refreshOnFocus: true })

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
  const ring = useCallback((runners: boolean) => {
    if (runners) wantRunners.current = true
    if (pending.current) return
    const wait = Math.max(0, lastFetch.current + DOORBELL_MIN_MS - Date.now())
    pending.current = setTimeout(() => {
      pending.current = null
      const fleet = wantRunners.current
      wantRunners.current = false
      void load(fleet ? 'always' : 'never')
    }, wait)
  }, [load])
  const seen = useRef<FrameMemo>({ status: new Map(), sessions: null })
  const onFrame = useCallback((raw: unknown) => {
    const change = frameChange(raw, seen.current)
    if (change !== 'none') ring(change === 'all')
  }, [ring])
  const onBadFrame = useCallback(() => ring(true), [ring])
  useEffect(() => () => {
    if (pending.current) clearTimeout(pending.current)
  }, [])
  const { connected } = useReconnectingSocket(wsUrl('ws/supervisor/'), onFrame, { onBadFrame })
  useEffect(() => {
    liveRef.current = connected
  }, [connected])

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
    trackClose,
    refresh,
    live: connected,
  }
}
