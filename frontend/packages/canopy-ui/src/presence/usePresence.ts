import { useEffect, useRef, useState } from 'react'
import type { PageLocation } from './pageKey'

export interface Viewer {
  email: string
  name: string
  subLocation: string
  idle: boolean
  self: boolean
}

export interface UsePresenceOptions {
  url: string
  location: PageLocation | null
}

const HEARTBEAT_MS = 20_000
const IDLE_AFTER_MS = 120_000
const RECONNECT_MS = 2_000

/**
 * One presence socket per tab.
 *
 * Navigation re-keys the existing connection with a fresh `presence.enter`
 * rather than reconnecting — a socket per page would churn handshakes on
 * every click.
 *
 * Every failure path degrades to an empty roster. Presence is an
 * enhancement; it must never surface an error to the user.
 */
export function usePresence({ url, location }: UsePresenceOptions): { viewers: Viewer[] } {
  const [viewers, setViewers] = useState<Viewer[]>([])
  const wsRef = useRef<WebSocket | null>(null)
  const locationRef = useRef(location)
  const idleRef = useRef(false)
  locationRef.current = location

  // Socket lifecycle. Deliberately NOT keyed on `location` — the socket
  // outlives navigation.
  useEffect(() => {
    if (!location) {
      setViewers([])
      return
    }
    let closedByCleanup = false
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let heartbeat: ReturnType<typeof setInterval> | null = null

    const send = (frame: unknown) => {
      const ws = wsRef.current
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(frame))
    }

    const enter = () => {
      const loc = locationRef.current
      if (loc) send({ type: 'presence.enter', page_key: loc.pageKey, sub_location: loc.subLocation })
    }

    function open() {
      let sock: WebSocket
      try {
        sock = new WebSocket(url)
      } catch {
        // Synchronous construction failure (malformed URL, mixed-content
        // scheme mismatch, …): treat exactly like any other connection
        // failure — stay on the empty roster and retry on the normal
        // reconnect cadence, never propagate.
        setViewers([])
        if (!closedByCleanup) reconnectTimer = setTimeout(open, RECONNECT_MS)
        return
      }
      wsRef.current = sock
      sock.onopen = () => {
        enter()
        heartbeat = setInterval(
          () => send({ type: 'presence.heartbeat', idle: idleRef.current }),
          HEARTBEAT_MS,
        )
      }
      sock.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data)
          if (msg.event !== 'presence.roster') return
          // Drop rosters for a page we have already navigated away from —
          // an in-flight broadcast can land after the re-key.
          if (msg.data?.page_key !== locationRef.current?.pageKey) return
          setViewers(
            (msg.data.viewers ?? []).map((v: Record<string, unknown>) => ({
              email: String(v.email ?? ''),
              name: String(v.name ?? ''),
              subLocation: String(v.sub_location ?? ''),
              idle: Boolean(v.idle),
              self: Boolean(v.self),
            })),
          )
        } catch {
          // Malformed frame: ignore. Never surface.
        }
      }
      sock.onclose = () => {
        if (wsRef.current === sock) wsRef.current = null
        if (heartbeat) clearInterval(heartbeat)
        setViewers([])
        if (closedByCleanup) return
        reconnectTimer = setTimeout(open, RECONNECT_MS)
      }
    }
    open()

    return () => {
      closedByCleanup = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      if (heartbeat) clearInterval(heartbeat)
      wsRef.current?.close()
      wsRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, location === null])

  // Re-key on navigation. Deps are the primitive fields, not `location`
  // itself — callers (e.g. pageKeyFor) may return a fresh object each
  // render, and keying on identity would re-send presence.enter on every
  // render instead of only on an actual location change.
  useEffect(() => {
    const ws = wsRef.current
    if (!location || !ws || ws.readyState !== WebSocket.OPEN) return
    setViewers([])
    ws.send(
      JSON.stringify({
        type: 'presence.enter',
        page_key: location.pageKey,
        sub_location: location.subLocation,
      }),
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location?.pageKey, location?.subLocation])

  // Idle tracking: hidden for longer than IDLE_AFTER_MS. Reports an
  // observable fact (the tab is not frontmost), never an attention claim.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null
    const flush = () => {
      const ws = wsRef.current
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'presence.heartbeat', idle: idleRef.current }))
      }
    }
    const onVisibility = () => {
      if (document.hidden) {
        timer = setTimeout(() => {
          idleRef.current = true
          flush()
        }, IDLE_AFTER_MS)
      } else {
        if (timer) clearTimeout(timer)
        if (idleRef.current) {
          idleRef.current = false
          flush()
        }
      }
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      if (timer) clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [])

  return { viewers }
}
