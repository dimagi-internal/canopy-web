import type { JSX } from 'react'

/**
 * "Close failed: <reason>" with a Retry, beside a session the runner could not
 * close. The reason is the runner's own (`SessionOut.close_error`), reported
 * from every close path — the phone's and the menu bar's "Close sessions" —
 * so the person does not have to go and read a log on another machine.
 * Retry is the ordinary close again; a new request clears the reason, and the
 * runner reports a fresh one if this attempt fails too.
 */
export function CloseFailedNote({
  reason,
  onRetry,
  busy = false,
  testId,
  className = '',
}: {
  reason: string
  onRetry: () => void
  busy?: boolean
  testId?: string
  className?: string
}): JSX.Element {
  return (
    <p
      className={`flex flex-wrap items-center gap-x-2 text-[12px] text-destructive ${className}`}
      data-testid={testId}
    >
      <span className="min-w-0">
        <span className="font-medium">Close failed:</span> {reason}
      </span>
      <button
        type="button"
        onClick={onRetry}
        disabled={busy}
        className="font-medium underline underline-offset-2 hover:text-foreground disabled:opacity-50"
      >
        {busy ? 'Retrying…' : 'Retry'}
      </button>
    </p>
  )
}
