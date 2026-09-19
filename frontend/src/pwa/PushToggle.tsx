import type { JSX } from 'react'
import { QuietMinutes } from './QuietMinutes'
import { usePush } from './usePush'

/**
 * Explains before it asks. The browser's own prompt is one reflexive tap from a
 * PERMANENT block, so the button carries the reason and the prompt only follows
 * a deliberate click.
 */
export function PushToggle(): JSX.Element | null {
  const { supported, permission, subscribed, error, subscribe, unsubscribe } = usePush()

  if (!supported) return null

  if (permission === 'denied') {
    return (
      <p className="rounded-lg border border-border bg-card p-3 text-[12px] text-muted-foreground">
        Notifications are blocked for this site. Chrome only lets you undo that from its own site
        settings.
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-1.5">
      <button
        type="button"
        data-testid="push-toggle"
        onClick={subscribed ? unsubscribe : subscribe}
        className="w-full rounded-lg border border-border bg-card px-3 py-2 text-[13px] font-medium text-foreground transition-colors hover:border-primary/40"
      >
        {subscribed ? 'Notifications on — tap to turn off' : 'Notify me when the fleet needs me'}
      </button>
      {subscribed && <QuietMinutes />}
      {error && <p className="text-[11px] text-destructive">{error}</p>}
    </div>
  )
}
