import { useState } from 'react'
import { useUpdateReady } from './updateReady'

/**
 * "A new version is ready — Reload", shown while a deployed build waits.
 *
 * Never reloads by itself: a visible page may hold something unsent (a draft,
 * a pasted sign-in code). Dismissing it hides it until the next deploy; the
 * hidden-tab auto-apply in registerPwa still switches over when you look away.
 */
export function UpdateBanner() {
  const apply = useUpdateReady()
  const [dismissed, setDismissed] = useState<(() => void) | null>(null)
  if (!apply || dismissed === apply) return null
  return (
    <div
      role="status"
      data-testid="update-banner"
      className="fixed bottom-3 left-3 z-50 flex items-center gap-3 rounded-md border border-border bg-popover px-3 py-2 text-[13px] text-popover-foreground shadow-lg"
    >
      <span>A new version of Canopy is ready.</span>
      <button
        type="button"
        onClick={apply}
        className="rounded bg-primary px-2 py-0.5 text-[12px] font-medium text-primary-foreground hover:bg-primary/90"
      >
        Reload
      </button>
      <button
        type="button"
        onClick={() => setDismissed(() => apply)}
        aria-label="Dismiss"
        className="text-muted-foreground hover:text-foreground"
      >
        ×
      </button>
    </div>
  )
}
