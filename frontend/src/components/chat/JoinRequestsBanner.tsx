import { useCallback, useEffect, useState } from 'react'
import { Button } from 'canopy-ui/ui'

import { declineJoinRequest, listJoinRequests, type JoinRequest } from '@/api/chat'
import { useVaultUnlock } from '@/vault/useVaultUnlock'
import { addPerson } from './ChatPeople'

/**
 * For a private conversation's owner: the people asking to be added
 * (`POST /{id}/join-requests`, from the interstitial). [Add them] is the ordinary
 * add-participant flow — this browser unwraps the conversation's key and wraps it
 * to theirs, which canopy-web cannot do — and that answers the request. Nothing
 * here for anyone else (the list is empty for them).
 */
export function JoinRequestsBanner({ sessionId }: { sessionId: string }) {
  const vault = useVaultUnlock()
  const [rows, setRows] = useState<JoinRequest[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState('')

  const load = useCallback(() => {
    listJoinRequests(sessionId).then(setRows).catch(() => setRows([]))
  }, [sessionId])
  useEffect(load, [load])

  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusy(id)
    setError('')
    try {
      await fn()
      load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'That did not work.')
    } finally {
      setBusy(null)
    }
  }

  if (rows.length === 0 && !error) return null
  return (
    <div data-testid="join-requests" className="border-b border-border bg-muted/40 px-3 py-1.5 text-[12px]">
      {rows.map((r) => (
        <div key={r.id} className="flex flex-wrap items-center gap-2 py-0.5">
          <span className="min-w-0 flex-1 truncate text-foreground">
            {r.requested_by_name || r.requested_by} asked to join this conversation
          </span>
          <Button size="sm" disabled={busy !== null} data-testid={`join-add-${r.id}`}
            onClick={() => void act(r.id, () => addPerson(sessionId, r.requested_by, 'editor', vault.unlock))}>
            Add them
          </Button>
          <Button size="sm" variant="outline" disabled={busy !== null} data-testid={`join-decline-${r.id}`}
            onClick={() => void act(r.id, () => declineJoinRequest(r.id))}>
            Decline
          </Button>
        </div>
      ))}
      {vault.element}
      {error && <p className="m-0 text-destructive">{error}</p>}
    </div>
  )
}
