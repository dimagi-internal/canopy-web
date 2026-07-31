import { useState } from 'react'
import { setAgentTurnMode, type TurnMode } from '@/api/agents'

const MODES: { mode: TurnMode; label: string; blurb: string }[] = [
  { mode: 'gated', label: 'Gated', blurb: 'Outbound actions wait for your approval.' },
  { mode: 'auto', label: 'Auto', blurb: 'Self-review-and-send; audit lands here on the board.' },
]

// The board-side autonomy switch. Turn mode is operational STATE, not repo
// config: the fleet turn procedure reads it at preflight, and this toggle is
// the one place a human flips it (the agent's own repo publish can't).
export function TurnModeToggle({
  agentSlug,
  initialMode,
}: {
  agentSlug: string
  initialMode: TurnMode
}) {
  const [mode, setMode] = useState<TurnMode>(initialMode)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const flip = async (next: TurnMode) => {
    if (next === mode || busy) return
    setBusy(true)
    setError(null)
    const prev = mode
    setMode(next)
    try {
      await setAgentTurnMode(agentSlug, next)
    } catch (e: unknown) {
      setMode(prev)
      setError(e instanceof Error ? e.message : 'Failed to set turn mode')
    } finally {
      setBusy(false)
    }
  }

  const active = MODES.find((m) => m.mode === mode)

  return (
    <div>
      <div className="inline-flex rounded-md border border-border bg-input p-0.5" role="radiogroup" aria-label="Turn mode">
        {MODES.map((m) => (
          <button
            key={m.mode}
            type="button"
            role="radio"
            aria-checked={mode === m.mode}
            disabled={busy}
            onClick={() => void flip(m.mode)}
            data-testid={`turn-mode-${m.mode}`}
            className={`rounded px-3 py-1 text-[12px] font-medium transition-colors disabled:opacity-60 ${
              mode === m.mode
                ? 'bg-primary text-primary-foreground'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            {m.label}
          </button>
        ))}
      </div>
      {active && <p className="mt-1 text-[11px] text-muted-foreground">{active.blurb}</p>}
      {error && <p className="mt-1 text-[11px] text-destructive">{error}</p>}
    </div>
  )
}
