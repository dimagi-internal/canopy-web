import type { JSX } from 'react'

// Which mode this session runs in — fixed when it started, so its first claimed
// turn's (apps/harness/turn_mode.py). Read-only: the agent-level switch is
// TurnModeChip (supervisor/AgentKpiCard). The sessions list shows every session, auto ones included, so this is how a manual session
// waiting on you is told apart from an agent's own auto run. Nothing renders
// until a turn has been claimed (`turn_mode` is "" then).
export function TurnModeBadge({ mode, testId }: { mode: string | undefined; testId?: string }): JSX.Element | null {
  if (mode !== 'manual' && mode !== 'auto') return null
  return (
    <span
      data-testid={testId}
      title={
        mode === 'auto'
          ? 'Auto mode: the agent acts without waiting for approval'
          : 'Manual mode: the agent waits for your approval before outbound actions'
      }
      className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${
        mode === 'auto' ? 'bg-primary/10 text-primary' : 'bg-muted text-foreground'
      }`}
    >
      {mode}
    </span>
  )
}
