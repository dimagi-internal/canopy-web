import { useEffect, useRef, useState, type ReactNode } from 'react'
import { UserPlus } from 'lucide-react'
import { Button } from 'canopy-ui/ui'

import type { JoinRequest } from '@/api/chat'

/**
 * Someone asking to join a PRIVATE conversation, shown IN that conversation to its
 * people (never on Home): "<who> asked to join this conversation · [Add them] [Decline]".
 *
 * The list is read once on open (`GET /{id}/join-requests`, empty for anyone not in
 * it) and then kept live by the chat socket's `session.join_requests` frame, which
 * carries the whole open list (`joinRequestsFrame`) — so an open chat shows a new
 * request, or loses an answered one, without a reload. The request names who asked,
 * never anything the conversation said.
 *
 * [Add them] is the ordinary add-participant flow (`actions.add`): this browser
 * unlocks its vault key, opens the conversation's key and wraps it to theirs, which
 * canopy-web cannot do. That add answers the request. Only the conversation's owner
 * (`canDecide`) answers; everyone else in it sees the request and who can.
 *
 * One component for canopy's chat page (and /ide's pane, which is that page) and the
 * embedded panel: each supplies `actions` through its own credentials.
 */

/** The socket frame that carries the open list. */
export const JOIN_REQUESTS_EVENT = 'session.join_requests'

/** The open list a `session.join_requests` frame carries; null for any other frame. */
export function joinRequestsFrame(frame: { event: string; data?: unknown }): JoinRequest[] | null {
  if (frame.event !== JOIN_REQUESTS_EVENT) return null
  const rows = (frame.data as { requests?: unknown } | undefined)?.requests
  return Array.isArray(rows) ? (rows as JoinRequest[]) : []
}

export interface JoinRequestActions {
  /** The open requests (empty for anyone not in the conversation). */
  list: () => Promise<JoinRequest[]>
  /** Add them: unlock, unwrap, wrap to them, `POST /{id}/participants`. */
  add: (email: string) => Promise<unknown>
  decline: (requestId: string) => Promise<unknown>
}

export function JoinRequests({ sessionId, actions, live, canDecide, unlockElement }: {
  sessionId: string
  actions: JoinRequestActions
  /** The latest list the socket carried; null until a frame arrives. */
  live: JoinRequest[] | null
  /** Its owner, who may add someone. */
  canDecide: boolean
  /** The vault unlock prompt, when the host does not render it elsewhere. */
  unlockElement?: ReactNode
}) {
  const [rows, setRows] = useState<JoinRequest[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState('')
  const actionsRef = useRef(actions)
  actionsRef.current = actions

  useEffect(() => {
    let current = true
    actionsRef.current.list()
      .then((r) => current && setRows(Array.isArray(r) ? r : []))
      .catch(() => current && setRows([]))
    return () => {
      current = false
    }
  }, [sessionId])
  useEffect(() => {
    if (live) setRows(live)
  }, [live])

  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id)
    setError('')
    try {
      await fn()
      // The socket confirms it with the new list; until then it is answered here.
      setRows((prev) => prev.filter((r) => r.id !== id))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'That did not work.')
    } finally {
      setBusy(null)
    }
  }

  if (rows.length === 0 && !error) return null
  return (
    <div data-testid="join-requests" className="border-b border-border bg-card px-3 py-2 text-[12px]">
      {rows.map((r) => (
        <div key={r.id} className="flex flex-wrap items-center gap-2 py-0.5" data-testid="join-request">
          <UserPlus className="h-3.5 w-3.5 shrink-0 text-primary" aria-hidden />
          <span className="min-w-0 flex-1 text-foreground">
            <span className="font-medium">{r.requested_by_name || r.requested_by}</span>
            {' asked to join this conversation'}
            {!canDecide && <span className="text-muted-foreground"> · its owner can add them</span>}
          </span>
          {canDecide && (
            <>
              <span className="text-muted-foreground" aria-hidden>·</span>
              <Button size="sm" disabled={busy !== null} data-testid={`join-add-${r.id}`}
                onClick={() => void act(r.id, () => actions.add(r.requested_by))}>
                {busy === r.id ? 'Adding…' : 'Add them'}
              </Button>
              <Button size="sm" variant="outline" disabled={busy !== null} data-testid={`join-decline-${r.id}`}
                onClick={() => void act(r.id, () => actions.decline(r.id))}>
                Decline
              </Button>
            </>
          )}
        </div>
      ))}
      {unlockElement}
      {error && <p className="m-0 mt-1 text-destructive">{error}</p>}
    </div>
  )
}
