import { useState, type JSX } from 'react'
import { holdFleet, releaseFleetHold, type FleetHoldOut } from '@/api/harness'

// The fleet hold (apps/harness/models.py::FleetHold) — the whole-fleet sibling of a
// runner's Pause. While held NO runner claims anything; turns wait queued with their
// trigger, which is the point: the queue becomes the list of what tried to start.
//
// Two audiences, one component:
// * a superuser gets the control — hold with a reason (`can_hold`), or release
//   (`can_release`); a named holder (Ada) may hold but never release, so an agent
//   can stop the fleet and only a person restarts it;
// * everyone else sees only the LOUD banner while it is on, so a member whose turn
//   is sitting queued can see why. Nothing at all when it is off.
//
// Holding stops every agent's work, so it takes a second tap to confirm. Releasing
// is the safe direction and is one tap.
// The page owns the polling (it needs `held` for the feed's alert line too); this
// renders `hold` and reports what the server returned after an action.
export function FleetHoldCard({
  hold,
  onChange,
}: {
  hold: FleetHoldOut | null
  onChange: (hold: FleetHoldOut) => void
}): JSX.Element | null {
  const [note, setNote] = useState('')
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const act = (call: Promise<FleetHoldOut>) => {
    setBusy(true)
    setErr(null)
    call
      .then((h) => {
        onChange(h)
        setConfirming(false)
        setNote('')
      })
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not change the fleet hold'))
      .finally(() => setBusy(false))
  }

  if (!hold || (!hold.held && !hold.can_hold)) return null

  if (hold.held) {
    const since = hold.held_at ? new Date(hold.held_at).toLocaleString() : ''
    return (
      <div
        role="alert"
        data-testid="fleet-hold-banner"
        className="flex flex-col gap-2 rounded-lg border-2 border-destructive bg-destructive/10 p-3 text-destructive"
      >
        <div className="flex items-center gap-2">
          <p className="text-[13px] font-bold uppercase tracking-wide">⏸ Fleet on hold — no runner starts anything</p>
          {hold.can_release && (
            <button
              type="button"
              onClick={() => act(releaseFleetHold())}
              disabled={busy}
              data-testid="fleet-hold-release"
              className="ml-auto rounded-md bg-primary px-2.5 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-50"
            >
              {busy ? '…' : 'Release'}
            </button>
          )}
        </div>
        <p className="text-[12px] leading-snug opacity-90" data-testid="fleet-hold-why">
          {hold.note || 'No reason given.'}
          {hold.held_by_email ? ` — ${hold.held_by_email}` : ''}
          {since ? `, since ${since}` : ''}.
        </p>
        <p className="text-[12px] leading-snug opacity-90">
          {hold.queued} turn{hold.queued === 1 ? '' : 's'} waiting. Running sessions finish normally; new ones
          queue with their trigger until the hold is released.
        </p>
        {err && <p className="text-[12px]" data-testid="fleet-hold-error">{err}</p>}
      </div>
    )
  }

  // Not held, and the caller may hold: the control.
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3" data-testid="fleet-hold-control">
      <div className="flex items-center gap-2">
        <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Fleet hold · system admins</span>
        {confirming ? (
          <span className="ml-auto flex gap-1.5">
            <button
              type="button"
              onClick={() => setConfirming(false)}
              disabled={busy}
              className="rounded-md border border-border px-2.5 py-1 text-[12px] text-foreground hover:bg-muted"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={() => act(holdFleet(note.trim()))}
              disabled={busy}
              data-testid="fleet-hold-confirm"
              className="rounded-md bg-destructive px-2.5 py-1 text-[12px] font-medium text-white disabled:opacity-50"
            >
              {busy ? '…' : 'Hold every runner'}
            </button>
          </span>
        ) : (
          <button
            type="button"
            onClick={() => setConfirming(true)}
            data-testid="fleet-hold-start"
            className="ml-auto rounded-md border border-border px-2.5 py-1 text-[12px] font-medium text-foreground hover:bg-muted"
          >
            Hold fleet
          </button>
        )}
      </div>
      <p className="text-[12px] text-muted-foreground">
        {confirming
          ? 'Every runner, in every workspace, stops starting sessions until you release. Running sessions finish.'
          : 'Stop every runner from starting new sessions, so you can see what tries to start.'}
      </p>
      <input
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Why? (e.g. tracing unexpected sessions)"
        maxLength={500}
        data-testid="fleet-hold-note"
        className="rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground placeholder:text-muted-foreground dark:placeholder:text-foreground-secondary"
      />
      {err && <p className="text-[12px] text-destructive" data-testid="fleet-hold-error">{err}</p>}
    </div>
  )
}
