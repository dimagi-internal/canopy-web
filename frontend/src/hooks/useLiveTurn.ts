import { useEffect, useRef, useState } from 'react'

import type { TurnEventFrame } from '@/api/types.ws'
import { wsUrl } from '@/lib/wsUrl'

import { useReconnectingSocket } from './useReconnectingSocket'

// Pure merge: append only unseen seqs, keep the list ordered by seq. Idempotent,
// so replay-then-live-tail and reconnect races never double-insert. Exported so
// it unit-tests without a socket (this repo's "no live DOM" test convention).
export function mergeEvents(prev: TurnEventFrame[], incoming: TurnEventFrame[]): TurnEventFrame[] {
  const seen = new Set(prev.map((e) => e.seq))
  const merged = prev.slice()
  for (const ev of incoming) {
    if (!seen.has(ev.seq)) {
      seen.add(ev.seq)
      merged.push(ev)
    }
  }
  merged.sort((a, b) => a.seq - b.seq)
  return merged
}

export interface LiveTurn {
  events: TurnEventFrame[]
  connected: boolean
  lastError: string | null
}

// Live-tail one turn's TurnEvent ledger over the realtime WS. Reconnects with
// backoff and resumes from the highest seq seen (?after=cursor), so a drop never
// re-fetches the whole ledger. The turn view / SP2 chat consume this.
export function useLiveTurn(turnId: string | null): LiveTurn {
  const [events, setEvents] = useState<TurnEventFrame[]>([])
  const [lastError, setLastError] = useState<string | null>(null)
  const cursorRef = useRef(0)

  // Reset per-turn state. Declared before the socket so it runs first.
  useEffect(() => {
    if (!turnId) return
    setEvents([])
    cursorRef.current = 0
  }, [turnId])

  const { connected } = useReconnectingSocket(
    turnId ? wsUrl(`ws/turns/${turnId}/`) : null,
    (raw) => {
      const ev = (raw as { event?: TurnEventFrame }).event
      if (!ev) return
      cursorRef.current = Math.max(cursorRef.current, ev.seq)
      setEvents((prev) => mergeEvents(prev, [ev]))
    },
    {
      connectUrl: (url) => `${url}?after=${cursorRef.current}`,
      onOpen: () => setLastError(null),
      onError: () => setLastError('connection error'),
      onBadFrame: () => setLastError('bad frame'),
    },
  )

  return { events, connected, lastError }
}
