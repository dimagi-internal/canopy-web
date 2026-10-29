import { useState, type JSX } from 'react'
import { holdFleet, releaseFleetHold, stopFleet, type FleetHoldOut, type FleetStopOut } from '@/api/harness'

// The fleet controls — two separate things, because they answer different needs:
//
// * HOLD is a toggle (apps/harness/models.py::FleetHold). While on, NO runner claims
//   anything; turns wait queued with their trigger, which is the point: the queue
//   becomes the list of what tried to start. Running work finishes.
// * STOP EVERYTHING is a one-shot action (`/fleet-stop`): every session working right
//   now is stopped and every running turn cancelled. It does not turn the hold on —
//   new work can start again straight after, unless the hold is on too.
//
// Audiences: a superuser gets both (`can_hold`, `can_release`, `can_stop_running`); a
// named holder such as Ada may switch the hold ON but not off, and gets no stop — an
// agent can pause the fleet, only a person restarts it or cuts work off. Everyone else
// sees only the LOUD banner while the hold is on, so a member whose turn sits queued
// can see why. Nothing at all when it is off.
//
// Stopping cuts people's work off mid-turn, so it takes a second tap. The hold
// destroys nothing, so it is one tap each way.
// The page owns the polling; this renders `hold` and reports what the server returned.
export function FleetHoldCard({
  hold,
  onChange,
}: {
  hold: FleetHoldOut | null
  onChange: (hold: FleetHoldOut) => void
}): JSX.Element | null {
  const [note, setNote] = useState('')
  const [confirmingStop, setConfirmingStop] = useState(false)
  const [stopped, setStopped] = useState<FleetStopOut | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const run = <T,>(call: Promise<T>, done: (r: T) => void) => {
    setBusy(true)
    setErr(null)
    call
      .then(done)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not change the fleet'))
      .finally(() => setBusy(false))
  }

  if (!hold || (!hold.held && !hold.can_hold)) return null

  const since = hold.held_at ? new Date(hold.held_at).toLocaleString() : ''
  const banner = hold.held && (
    <div
      role="alert"
      data-testid="fleet-hold-banner"
      className="flex flex-col gap-1 rounded-lg border-2 border-destructive bg-destructive/10 p-3 text-destructive"
    >
      <p className="text-[13px] font-bold uppercase tracking-wide">⏸ Fleet on hold — no runner starts anything</p>
      <p className="text-[12px] leading-snug opacity-90" data-testid="fleet-hold-why">
        {hold.note || 'No reason given.'}
        {hold.held_by_email ? ` — ${hold.held_by_email}` : ''}
        {since ? `, since ${since}` : ''}.
      </p>
      <p className="text-[12px] leading-snug opacity-90">
        {hold.queued} turn{hold.queued === 1 ? '' : 's'} waiting. New sessions queue with their trigger until the
        hold is turned off.
      </p>
    </div>
  )

  // Not a holder: the banner only (while held).
  if (!hold.can_hold) return banner || null

  const mayToggle = hold.held ? hold.can_release : true
  const toggle = () =>
    run(hold.held ? releaseFleetHold() : holdFleet(note.trim()), (h) => {
      onChange(h)
      setNote('')
    })

  return (
    <div className="flex flex-col gap-2" data-testid="fleet-hold-control">
      {banner}
      <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3">
        <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Fleet controls · superusers</span>

        <div className="flex items-center gap-3">
          <button
            type="button"
            role="switch"
            aria-checked={hold.held}
            aria-label="Hold the fleet"
            onClick={toggle}
            disabled={busy || !mayToggle}
            data-testid="fleet-hold-toggle"
            className={`relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-50 ${
              hold.held ? 'bg-destructive' : 'bg-muted'
            }`}
          >
            <span
              className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all ${
                hold.held ? 'left-[22px]' : 'left-0.5'
              }`}
            />
          </button>
          <div className="flex flex-col">
            <span className="text-[13px] font-medium text-foreground">Hold the fleet</span>
            <span className="text-[12px] text-muted-foreground">
              {hold.held
                ? hold.can_release
                  ? 'On. Nothing new starts, in any workspace. Turn off to let the queue run.'
                  : 'On. Only a superuser can turn it off.'
                : 'Off. When on, no runner starts anything new; running sessions finish.'}
            </span>
          </div>
        </div>
        {!hold.held && (
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Why? (optional, shown to everyone while it is on)"
            maxLength={500}
            data-testid="fleet-hold-note"
            className="rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground placeholder:text-placeholder"
          />
        )}

        {hold.can_stop_running && (
          <div className="flex flex-col gap-1.5 border-t border-border pt-2">
            <div className="flex items-center gap-2">
              <div className="flex flex-col">
                <span className="text-[13px] font-medium text-foreground">Stop everything</span>
                <span className="text-[12px] text-muted-foreground">
                  {confirmingStop
                    ? 'Stops every working session and cancels every running turn, in every workspace, now.'
                    : 'Stop all running work now. Turn the hold on too, or new work can start again.'}
                </span>
              </div>
              {confirmingStop ? (
                <span className="ml-auto flex shrink-0 gap-1.5">
                  <button
                    type="button"
                    onClick={() => setConfirmingStop(false)}
                    disabled={busy}
                    className="rounded-md border border-border px-2.5 py-1 text-[12px] text-foreground hover:bg-muted"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      run(stopFleet(), (r) => {
                        setStopped(r)
                        setConfirmingStop(false)
                      })
                    }
                    disabled={busy}
                    data-testid="fleet-stop-confirm"
                    className="rounded-md bg-destructive px-2.5 py-1 text-[12px] font-bold text-white disabled:opacity-50"
                  >
                    {busy ? '…' : 'Stop everything now'}
                  </button>
                </span>
              ) : (
                <button
                  type="button"
                  onClick={() => {
                    setStopped(null)
                    setConfirmingStop(true)
                  }}
                  data-testid="fleet-stop-start"
                  className="ml-auto shrink-0 rounded-md border border-destructive px-2.5 py-1 text-[12px] font-medium text-destructive hover:bg-destructive/10"
                >
                  Stop everything
                </button>
              )}
            </div>
            {stopped && (
              <p className="text-[12px] font-medium text-foreground" data-testid="fleet-stopped">
                Stopped {stopped.stopped_sessions} working session{stopped.stopped_sessions === 1 ? '' : 's'} and{' '}
                {stopped.stopped_turns} more running turn{stopped.stopped_turns === 1 ? '' : 's'}.
              </p>
            )}
          </div>
        )}
        {err && <p className="text-[12px] text-destructive" data-testid="fleet-hold-error">{err}</p>}
      </div>
    </div>
  )
}
