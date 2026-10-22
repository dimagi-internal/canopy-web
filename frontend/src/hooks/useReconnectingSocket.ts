import { useEffect, useRef, useState } from 'react'

/** The reconnect ladder every live socket in the app walks: 1s, 2s, 5s, then 10s
 *  for as long as it stays down. A successful open resets it. */
export const RECONNECT_BACKOFFS_MS = [1000, 2000, 5000, 10000] as const

export interface ReconnectingSocketOptions {
  /** Called on every (re)connect to build the URL actually dialled — for a
   *  socket that resumes from a cursor (`?after=<seq>`). Defaults to `url`. */
  connectUrl?: (url: string) => string
  /** A frame that is not JSON. Ignored when absent. */
  onBadFrame?: (raw: unknown) => void
  onOpen?: () => void
  onError?: () => void
}

/** One WebSocket that reconnects with backoff until unmount (or `url` changes,
 *  which closes the old socket and dials the new one; `null` dials nothing).
 *  Each message is JSON-parsed and handed to `onFrame`; the callbacks may change
 *  identity freely — the latest one is always called, without a reconnect.
 *  Returns whether the socket is open right now. */
export function useReconnectingSocket(
  url: string | null,
  onFrame: (frame: unknown) => void,
  opts: ReconnectingSocketOptions = {},
): { connected: boolean } {
  const [connected, setConnected] = useState(false)
  const handlers = useRef({ onFrame, ...opts })
  handlers.current = { onFrame, ...opts }

  useEffect(() => {
    if (url === null) return
    let ws: WebSocket | null = null
    let timer: ReturnType<typeof setTimeout> | null = null
    let closed = false
    let attempt = 0

    const connect = () => {
      if (closed) return
      const h = handlers.current
      ws = new WebSocket(h.connectUrl ? h.connectUrl(url) : url)
      ws.onopen = () => {
        attempt = 0
        setConnected(true)
        handlers.current.onOpen?.()
      }
      ws.onmessage = (msg: MessageEvent) => {
        let frame: unknown
        try {
          frame = JSON.parse(msg.data as string)
        } catch {
          handlers.current.onBadFrame?.(msg.data)
          return
        }
        handlers.current.onFrame(frame)
      }
      ws.onerror = () => handlers.current.onError?.()
      ws.onclose = () => {
        setConnected(false)
        if (closed) return
        const delay = RECONNECT_BACKOFFS_MS[Math.min(attempt, RECONNECT_BACKOFFS_MS.length - 1)]
        attempt += 1
        timer = setTimeout(connect, delay)
      }
    }

    connect()
    return () => {
      closed = true
      if (timer) clearTimeout(timer)
      ws?.close()
      setConnected(false)
    }
  }, [url])

  return { connected }
}
