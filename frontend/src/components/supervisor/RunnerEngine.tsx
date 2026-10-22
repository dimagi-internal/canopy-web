import { useState, type JSX } from 'react'
import { setRunnerEngine, type RunnerEngine as Engine, type RunnerOut } from '@/api/harness'
import { Choice, SettingRow } from '@/components/supervisor/SettingRow'

// The session runtime switch (canopy-web#1188): whether this laptop runner opens
// NEW sessions in emdash, in the Claude desktop app's Code tab, or runs them
// itself headless (`claude-cli`, which a person can later open in the desktop
// app). The runner reads the setting off its next heartbeat (a few seconds), so
// there is nothing to restart; sessions already running keep going where they
// are, and routing is the same either way. Owner or runner admins, like flags.
const LABEL: Record<Engine, string> = {
  emdash: 'emdash',
  'claude-desktop': 'Claude desktop',
  'claude-cli': 'Claude CLI',
}

export function RunnerEngine({
  runner,
  onChange,
}: {
  runner: RunnerOut
  onChange: (fresh: RunnerOut) => void
}): JSX.Element {
  const current = (runner.engine || 'emdash') as Engine
  const [saving, setSaving] = useState<Engine | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const choose = (engine: Engine) => {
    if (saving) return
    setSaving(engine)
    setErr(null)
    setRunnerEngine(runner.id, engine)
      .then(onChange)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not change the runtime'))
      .finally(() => setSaving(null))
  }

  return (
    <SettingRow
      label="Session runtime"
      testId="runner-engine"
      help="Where new sessions open. Takes effect within seconds; running sessions stay where they are."
      control={<Choice value={current} options={LABEL} onChange={choose} saving={saving} label="Session runtime" testIdPrefix="runner-engine" />}
    >
      {err && <p className="text-[12px] text-destructive" data-testid="runner-engine-error">{err}</p>}
    </SettingRow>
  )
}
