import { useState } from 'react'
import { setAgentPeopleDigestEnabled } from '@/api/agents'

// Agent-admin switch for the people digest (fleet brain v1.1, canopy#804):
// whether a person's finished conversation with this agent starts the turn
// that records work-context facts about them. On by default.
export function PeopleDigestToggle({
  agentSlug,
  initialEnabled,
  canEdit,
}: {
  agentSlug: string
  initialEnabled: boolean
  canEdit: boolean
}) {
  const [enabled, setEnabled] = useState(initialEnabled)
  const [globallyOff, setGloballyOff] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const flip = async (next: boolean) => {
    if (next === enabled || busy || !canEdit) return
    setBusy(true)
    setError(null)
    setEnabled(next)
    try {
      const r = await setAgentPeopleDigestEnabled(agentSlug, next)
      setGloballyOff(!r.globally_enabled)
    } catch (e: unknown) {
      setEnabled(!next)
      setError(e instanceof Error ? e.message : 'Failed to change the people digest')
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
      <div className="inline-flex rounded-md border border-border bg-input p-0.5" role="radiogroup" aria-label="People digest">
        {options.map((o) => (
          <button
            key={o.label}
            type="button"
            role="radio"
            aria-checked={enabled === o.value}
            disabled={busy || !canEdit}
            onClick={() => void flip(o.value)}
            data-testid={`people-digest-${o.label.toLowerCase()}`}
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
      {globallyOff && (
        <p className="mt-1 text-[11px] text-warning">
          The people digest is switched off for the whole deployment, so no agent runs it right now.
        </p>
      )}
    </div>
  )
}
