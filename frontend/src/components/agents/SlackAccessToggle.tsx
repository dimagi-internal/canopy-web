import { useState } from 'react'
import { setAgentSlackEnabled } from '@/api/agents'

// Owner-only switch for Slack access (apps/slack). Off by default: turning it
// on opens the agent to everyone in the connected Slack who has linked their
// canopy account, so it is a deliberate act, not repo config.
export function SlackAccessToggle({
  agentSlug,
  initialEnabled,
}: {
  agentSlug: string
  initialEnabled: boolean
}) {
  const [enabled, setEnabled] = useState(initialEnabled)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const flip = async (next: boolean) => {
    if (next === enabled || busy) return
    setBusy(true)
    setError(null)
    setEnabled(next)
    try {
      await setAgentSlackEnabled(agentSlug, next)
    } catch (e: unknown) {
      setEnabled(!next)
      setError(e instanceof Error ? e.message : 'Failed to change Slack access')
    } finally {
      setBusy(false)
    }
  }

  const options: { value: boolean; label: string }[] = [
    { value: false, label: 'Off' },
    { value: true, label: 'On' },
  ]

  return (
    <div>
      <div className="inline-flex rounded-md border border-border bg-input p-0.5" role="radiogroup" aria-label="Slack access">
        {options.map((o) => (
          <button
            key={o.label}
            type="button"
            role="radio"
            aria-checked={enabled === o.value}
            disabled={busy}
            onClick={() => void flip(o.value)}
            data-testid={`slack-access-${o.label.toLowerCase()}`}
            className={`rounded px-3 py-1 text-[12px] font-medium transition-colors disabled:opacity-60 ${
              enabled === o.value
                ? 'bg-primary text-primary-foreground'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            {o.label}
          </button>
        ))}
      </div>
      {error && <p className="mt-1 text-[11px] text-destructive">{error}</p>}
    </div>
  )
}
