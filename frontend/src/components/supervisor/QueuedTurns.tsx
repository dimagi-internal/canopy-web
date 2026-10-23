import { useState, type JSX } from 'react'
import { cancelTurn, type UnclaimableTurn } from '@/api/harness'

// Queued turns no runner has taken, and why — a fact list, not an alert. A turn
// addressed to an agent or repo nothing online declares would otherwise sit with
// no signal (one sat 12h), and a turn pinned to a box that is off waits for that
// one box. The agent turns that need a routing fix are also the
// `unclaimable_turns` alert (docs/architecture/alerts.md), which is what Home,
// the badge and notifications follow; this list keeps every turn and its Cancel.
export function QueuedTurns({
  turns,
  onCancelled,
}: {
  turns: UnclaimableTurn[]
  /** Called with the ids that are now cancelled, to drop them from the list. */
  onCancelled: (ids: string[]) => void
}): JSX.Element | null {
  const [busy, setBusy] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  if (turns.length === 0) return null

  const cancellable = turns.filter((t) => t.can_cancel)
  const cancel = async (ids: string[], key: string) => {
    setBusy(key)
    setErr(null)
    const done: string[] = []
    for (const id of ids) {
      try {
        await cancelTurn(id)
        done.push(id)
      } catch (e: unknown) {
        setErr(e instanceof Error ? e.message : 'could not cancel it')
      }
    }
    setBusy(null)
    if (done.length) onCancelled(done)
  }

  const n = turns.length
  const s = n === 1 ? '' : 's'
  const parked = turns.every((t) => t.kind === 'parked')
  const waiting = turns.every((t) => t.kind === 'offline' || t.kind === 'parked')
  return (
    <div
      data-testid="queued-turns"
      className="rounded-lg border border-border bg-card p-3 text-foreground"
    >
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-[13px] font-semibold">
          {parked
            ? `${n} queued turn${s} waiting on a paused runner`
            : waiting
            ? `${n} queued turn${s} waiting on an unreachable runner`
            : `${n} queued turn${s} no runner can claim`}
        </p>
        {cancellable.length > 1 && (
          <button
            type="button"
            data-testid="stuck-cancel-all"
            disabled={busy !== null}
            onClick={() => void cancel(cancellable.map((t) => t.turn_id), 'all')}
            className="ml-auto min-h-11 rounded-md border border-input px-2.5 py-1 text-[12px] font-medium hover:bg-muted disabled:opacity-50 sm:min-h-0"
          >
            {busy === 'all' ? 'Cancelling…' : `Cancel all ${cancellable.length}`}
          </button>
        )}
      </div>
      <ul className="mt-1 space-y-1.5">
        {turns.map((t) => (
          <li key={t.turn_id} className="flex items-start gap-2 text-[13px] leading-snug">
            <div className="min-w-0 flex-1">
              <span className="rounded bg-muted px-1 font-mono font-semibold">{t.target}</span>{' '}
              {t.prompt ? <span className="opacity-90">“{t.prompt}”</span> : null}
              <span className="block text-[12px] text-muted-foreground">{t.reason}</span>
            </div>
            {t.can_cancel && (
              <button
                type="button"
                data-testid={`stuck-cancel-${t.turn_id}`}
                disabled={busy !== null}
                onClick={() => void cancel([t.turn_id], t.turn_id)}
                className="min-h-11 shrink-0 rounded-md border border-input px-2 py-0.5 text-[12px] hover:bg-muted disabled:opacity-50 sm:min-h-0"
              >
                {busy === t.turn_id ? '…' : 'Cancel'}
              </button>
            )}
          </li>
        ))}
      </ul>
      <p className="mt-1.5 text-[12px] leading-snug text-muted-foreground">
        {parked
          ? 'These run when their runner is resumed (below), or at the resume time it was paused until.'
          : waiting
          ? 'These run as soon as their runner reconnects. A readiness check that waits a day expires on its own.'
          : 'Fix the routing (below) or cancel it — it will not run otherwise.'}
      </p>
      {err && <p className="mt-1 text-[12px] text-destructive" data-testid="stuck-cancel-error">{err}</p>}
    </div>
  )
}
