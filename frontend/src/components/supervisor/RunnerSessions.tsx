import { useState, type JSX } from 'react'
import { setRunnerSessions, type RunnerOut } from '@/api/harness'
import { isDirectedOnly } from '@/components/chat/runnerEligibility'
import { SettingRow, Switch } from '@/components/supervisor/SettingRow'

// Slack/chat sessions on or off. Was settable only over the API, so a box paired
// before pairing set it (haldimagi-mbp-cdp, 2026-10-02) silently never appeared
// in "start a session" and nobody could see why. The switch is the owner's; anyone
// else reads the state, because "why isn't this box in the session picker?" is a
// question any member asks.
export function RunnerSessions({
  runner,
  onChanged,
}: {
  runner: RunnerOut
  onChanged?: (fresh: RunnerOut) => void
}): JSX.Element {
  const on = Boolean((runner.capabilities as { sessions?: boolean } | null)?.sessions)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const toggle = () => {
    setSaving(true)
    setErr(null)
    setRunnerSessions(runner, !on)
      .then((fresh) => onChanged?.(fresh))
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not change session setting'))
      .finally(() => setSaving(false))
  }

  return (
    <SettingRow
      label="Slack & chat sessions"
      testId="runner-sessions"
      help={
        on
          ? 'Takes Slack threads and web chat, and appears in "start a session".'
          : 'Off — this box never takes Slack or chat work and is hidden from "start a session".'
      }
      control={
        runner.can_manage ? (
          <Switch checked={on} onChange={toggle} disabled={saving} label="Slack & chat sessions" testId="runner-sessions-toggle" />
        ) : (
          <span className="text-[12px] text-foreground" data-testid="runner-sessions-state">{on ? 'on' : 'off'}</span>
        )
      }
    >
      {isDirectedOnly(runner) && (
        <p className="text-[12px] text-foreground" data-testid="runner-directed">
          Directed only: takes turns pinned to it and chats started on it, and nothing routed — no
          agent, repo or unrouted chat work, even where a runner order or an agent's list names it.
        </p>
      )}
      {err && <p className="text-[12px] text-destructive" data-testid="runner-sessions-error">{err}</p>}
    </SettingRow>
  )
}
