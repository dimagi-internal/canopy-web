import { useState } from 'react'
import {
  Button,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from 'canopy-ui/ui'
import { transferSession, type ChatSession, type TransferResult } from '@/api/chat'
import { listRunners, type RunnerOut } from '@/api/harness'
import { onlineSessionCapableRunners } from './runnerEligibility'

/**
 * "Transfer to…" — move a session onto another runner, picked from the
 * fleet. A thin UI over POST /{id}/transfer (apps/canopy_sessions/transfer_requests.py),
 * which already decides whether the move happens now or waits on the
 * target's administrator; this only has to offer a runner and show what
 * came back.
 *
 * Runners load lazily on first open rather than at panel mount — this sits
 * in a per-row list, and fetching the fleet once per row on every render
 * would be one request per session for data nobody asked to see yet.
 */
export function TransferSessionMenu({
  session,
  onResult,
}: {
  session: ChatSession
  onResult: (result: TransferResult | null, error: string | null) => void
}) {
  const [runners, setRunners] = useState<RunnerOut[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [selectedRunnerId, setSelectedRunnerId] = useState('')
  const [brief, setBrief] = useState('')
  const [sending, setSending] = useState(false)

  // Unbound — nothing to move off of (services.request raises LookupError for
  // exactly this), and archived sessions refuse with a ValueError. Neither is
  // worth a round trip to discover.
  const disabled = !session.runner_name || session.status !== 'active'

  const options = onlineSessionCapableRunners(runners ?? [], session.runner_requirements).filter(
    (r) => r.name !== session.runner_name,
  )

  const load = () => {
    if (runners !== null || loading) return
    setLoading(true)
    listRunners()
      .then(setRunners)
      .catch(() => setRunners([]))
      .finally(() => setLoading(false))
  }

  const reset = () => {
    setSelectedRunnerId('')
    setBrief('')
  }

  const confirm = () => {
    if (!selectedRunnerId) return
    setSending(true)
    transferSession(session.id, selectedRunnerId, brief)
      .then((result) => onResult(result, null))
      .catch((err: unknown) => onResult(null, err instanceof Error ? err.message : 'Could not transfer this session'))
      .finally(() => {
        setSending(false)
        reset()
      })
  }

  return (
    <DropdownMenu onOpenChange={(open: boolean) => { if (open) load(); else reset() }}>
      <DropdownMenuTrigger
        data-testid={`transfer-session-${session.id}`}
        aria-label={`Transfer ${session.title?.trim() || 'Untitled chat'} to another runner`}
        title={disabled ? 'Nothing to move — this session has no runner, or is archived' : 'Transfer to another runner'}
        disabled={disabled}
        className="shrink-0 px-2 text-muted-foreground hover:text-foreground disabled:opacity-40"
      >
        ⇄
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <div className="flex flex-col gap-2 px-2 py-1.5" data-testid="transfer-picker">
          <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
            Move to
            <select
              value={selectedRunnerId}
              onChange={(e) => setSelectedRunnerId(e.target.value)}
              disabled={loading}
              className="rounded-md border border-input bg-input px-1.5 py-1 text-[12px] text-foreground"
              data-testid="transfer-runner-select"
            >
              <option value="">{loading ? 'Loading runners…' : 'Pick a runner'}</option>
              {options.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </select>
          </label>
          {!loading && runners !== null && options.length === 0 && (
            <p className="text-[11px] text-muted-foreground">
              No other online, session-capable runner is eligible.
            </p>
          )}
          <label className="flex flex-col gap-1 text-[11px] text-muted-foreground">
            Handoff note (optional)
            <textarea
              value={brief}
              onChange={(e) => setBrief(e.target.value)}
              placeholder="What the new runner needs to know"
              rows={2}
              className="resize-none rounded-md border border-input bg-input px-1.5 py-1 text-[12px] text-foreground"
              data-testid="transfer-brief"
            />
          </label>
          <Button size="sm" disabled={!selectedRunnerId || sending} onClick={confirm}>
            {sending ? 'Transferring…' : 'Transfer'}
          </Button>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export default TransferSessionMenu
