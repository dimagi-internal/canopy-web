import { useState, type JSX } from 'react'
import { setRunnerPrivate, type RunnerOut } from '@/api/harness'
import { SettingRow, Switch } from '@/components/supervisor/SettingRow'

// A runner as its owner's PRIVATE runner (canopy-web#1705). Shared: the box takes
// fleet work as routed. Private: only what its owner pins or binds to it, plus the
// Claude Code sessions opened on that machine (recorded as the owner's local turns —
// the runner writes the registration itself, no CLI). Owner only.
//
// Running as a lower role is not here on purpose: it is a property of one turn
// (`acts_as`), or of a client that puts itself in that mode, never a setting left on.
export function RunnerPrivate({
  runner,
  onChange,
}: {
  runner: RunnerOut
  onChange: (fresh: RunnerOut) => void
}): JSX.Element {
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const toggle = () => {
    if (saving) return
    setSaving(true)
    setErr(null)
    setRunnerPrivate(runner.id, !runner.private)
      .then(onChange)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not change it'))
      .finally(() => setSaving(false))
  }

  return (
    <SettingRow
      label="Private"
      testId="runner-private"
      help={
        runner.private
          ? 'Does only your work (what you pin to it) and runs your own Claude Code sessions’ turns.'
          : 'Off — takes fleet work as routed.'
      }
      control={<Switch checked={!!runner.private} onChange={toggle} disabled={saving} label="Private" testId="runner-private-toggle" />}
    >
      {err && <p className="text-[12px] text-destructive" data-testid="runner-private-error">{err}</p>}
    </SettingRow>
  )
}
