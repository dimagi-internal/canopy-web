import { useState, type JSX } from 'react'
import type { FleetStopOut } from '@/api/harness'

// The two controls every scope has — the whole fleet (FleetHoldCard) and one
// workspace's tree (WorkspaceControlsCard): a HOLD that is a toggle (nothing new
// starts; running work finishes) and STOP EVERYTHING, a separate one-shot action that
// stops what is running now and does not turn the hold on. Stopping cuts people's
// work off mid-turn, so it takes a second tap; the hold destroys nothing, so it is
// one tap each way. The cards own the data and the copy; this is the controls.
export function HoldStopPanel({
  title,
  scope,
  held,
  mayToggle,
  heldHelp,
  onHold,
  onRelease,
  canStop,
  onStop,
  testid,
}: {
  title: string
  // What the controls act on, in words: "every workspace", "Dimagi and every workspace below it".
  scope: string
  held: boolean
  mayToggle: boolean
  // What "on" means for the caller (e.g. that only someone else can turn it off).
  heldHelp: string
  onHold: (note: string) => Promise<unknown>
  onRelease: () => Promise<unknown>
  canStop: boolean
  onStop: () => Promise<FleetStopOut>
  testid: string
}): JSX.Element {
  const [note, setNote] = useState('')
  const [confirmingStop, setConfirmingStop] = useState(false)
  const [stopped, setStopped] = useState<FleetStopOut | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const run = <T,>(call: () => Promise<T>, done: (r: T) => void) => {
    setBusy(true)
    setErr(null)
    call()
      .then(done)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not change it'))
      .finally(() => setBusy(false))
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3" data-testid={testid}>
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">{title}</span>

      <div className="flex items-center gap-3">
        <button
          type="button"
          role="switch"
          aria-checked={held}
          aria-label={`Hold ${scope}`}
          onClick={() => run(() => (held ? onRelease() : onHold(note.trim())), () => setNote(''))}
          disabled={busy || !mayToggle}
          data-testid={`${testid}-toggle`}
          className={`relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-50 ${
            held ? 'bg-destructive' : 'bg-muted'
          }`}
        >
          <span
            className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all ${
              held ? 'left-[22px]' : 'left-0.5'
            }`}
          />
        </button>
        <div className="flex flex-col">
          <span className="text-[13px] font-medium text-foreground">Hold</span>
          <span className="text-[12px] text-muted-foreground">
            {held ? heldHelp : `Off. When on, nothing new starts in ${scope}; running sessions finish.`}
          </span>
        </div>
      </div>
      {!held && mayToggle && (
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Why? (optional, shown to everyone while it is on)"
          maxLength={500}
          data-testid={`${testid}-note`}
          className="rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground placeholder:text-placeholder"
        />
      )}

      {canStop && (
        <div className="flex flex-col gap-1.5 border-t border-border pt-2">
          <div className="flex items-center gap-2">
            <div className="flex flex-col">
              <span className="text-[13px] font-medium text-foreground">Stop everything</span>
              <span className="text-[12px] text-muted-foreground">
                {confirmingStop
                  ? `Stops every working session and cancels every running turn in ${scope}, now.`
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
                    run(onStop, (r) => {
                      setStopped(r)
                      setConfirmingStop(false)
                    })
                  }
                  disabled={busy}
                  data-testid={`${testid}-stop-confirm`}
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
                data-testid={`${testid}-stop-start`}
                className="ml-auto shrink-0 rounded-md border border-destructive px-2.5 py-1 text-[12px] font-medium text-destructive hover:bg-destructive/10"
              >
                Stop everything
              </button>
            )}
          </div>
          {stopped && (
            <p className="text-[12px] font-medium text-foreground" data-testid={`${testid}-stopped`}>
              Stopped {stopped.stopped_sessions} working session{stopped.stopped_sessions === 1 ? '' : 's'} and{' '}
              {stopped.stopped_turns} more running turn{stopped.stopped_turns === 1 ? '' : 's'}.
            </p>
          )}
        </div>
      )}
      {err && <p className="text-[12px] text-destructive" data-testid={`${testid}-error`}>{err}</p>}
    </div>
  )
}

/** The loud banner while a hold is on — what a member whose turn sits queued sees. */
export function HoldBanner({
  headline,
  note,
  heldByEmail,
  heldAt,
  queued,
  testid,
}: {
  headline: string
  note: string
  heldByEmail: string
  heldAt: string | null | undefined
  queued: number
  testid: string
}): JSX.Element {
  const since = heldAt ? new Date(heldAt).toLocaleString() : ''
  return (
    <div
      role="alert"
      data-testid={testid}
      className="flex flex-col gap-1 rounded-lg border-2 border-destructive bg-destructive/10 p-3 text-destructive"
    >
      <p className="text-[13px] font-bold uppercase tracking-wide">⏸ {headline}</p>
      <p className="text-[12px] leading-snug opacity-90" data-testid={`${testid}-why`}>
        {note || 'No reason given.'}
        {heldByEmail ? ` — ${heldByEmail}` : ''}
        {since ? `, since ${since}` : ''}.
      </p>
      <p className="text-[12px] leading-snug opacity-90">
        {queued} turn{queued === 1 ? '' : 's'} waiting. New sessions queue with their trigger until the hold is
        turned off.
      </p>
    </div>
  )
}
