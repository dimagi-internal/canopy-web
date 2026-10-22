import { useState, type JSX } from 'react'
import { pauseRunner, unpauseRunner, type RunnerOut } from '@/api/harness'
import { SettingRow, Switch } from '@/components/supervisor/SettingRow'

// Pause — the remote half of ~/.canopy/PAUSED, and the only way to park a box from
// a phone. Owner-only: POST /pause resolves through _runner_visibility_q, the same
// predicate can_manage reports. Switching it off asks why first (one more click);
// switching it back on is one click and clears the reason server-side, because a
// reason for a pause that is over is just stale text.
export function RunnerPause({
  runner,
  onChanged,
}: {
  runner: RunnerOut
  onChanged?: (fresh: RunnerOut) => void
}): JSX.Element {
  const [asking, setAsking] = useState(false)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const run = (call: Promise<RunnerOut>) => {
    setBusy(true)
    setErr(null)
    call
      .then((fresh) => {
        setNote('')
        setAsking(false)
        onChanged?.(fresh)
      })
      // Say it failed. A pause that silently didn't take is the expensive
      // failure here — you walk away believing an account stopped spending.
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not change pause state'))
      .finally(() => setBusy(false))
  }

  const onSwitch = () => {
    if (runner.paused) run(unpauseRunner(runner.id))
    else setAsking((a) => !a)
  }

  return (
    <SettingRow
      label="Taking work"
      testId="runner-pause"
      help={
        runner.paused ? (
          <span data-testid="runner-pause-why">
            Paused{runner.paused_by_email ? <> by {runner.paused_by_email}</> : null}: {runner.paused_note || 'no reason given.'}{' '}
            {runner.unpause_at ? (
              <span data-testid="runner-unpause-at">
                Resumes on its own at {new Date(runner.unpause_at).toLocaleString()}; switch it on to let it claim sooner.
              </span>
            ) : (
              <>Work only this runner could take is not run, and the agent&apos;s owner is told.</>
            )}
          </span>
        ) : (
          'Claims the work routed to it. Switch off to pause.'
        )
      }
      control={<Switch checked={!runner.paused} onChange={onSwitch} disabled={busy} label="Taking work" testId="runner-pause-toggle" />}
    >
      {asking && !runner.paused && (
        <div className="flex gap-2">
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Why? (e.g. token limit on this account)"
            maxLength={200}
            autoFocus
            data-testid="runner-pause-note"
            className="min-w-0 flex-1 rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground placeholder:text-placeholder"
          />
          <button
            type="button"
            onClick={() => run(pauseRunner(runner.id, note.trim()))}
            disabled={busy}
            data-testid="runner-pause-confirm"
            className="rounded-md bg-primary px-2.5 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-50"
          >
            {busy ? '…' : 'Pause'}
          </button>
        </div>
      )}
      {err && <p className="text-[12px] text-destructive" data-testid="runner-pause-error">{err}</p>}
    </SettingRow>
  )
}
