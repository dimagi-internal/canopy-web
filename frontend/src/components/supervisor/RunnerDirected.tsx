import { useState, type JSX } from 'react'
import { setRunnerDirected, type RunnerOut } from '@/api/harness'
import { SettingRow, Switch } from '@/components/supervisor/SettingRow'

// Directed only (`Runner.directed`): the box takes only turns pinned to it and chats
// bound to it, no routed work. A runner administrator's setting — a test box
// (`up.sh --test`) pairs with it on — never something the box says about itself.
// A private runner is always directed.
export function RunnerDirected({
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
    setRunnerDirected(runner.id, !runner.directed)
      .then(onChange)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not change it'))
      .finally(() => setSaving(false))
  }

  return (
    <SettingRow
      label="Directed only"
      testId="runner-directed"
      help={
        (runner.directed
          ? 'Takes only turns pinned to it and chats started on it — no routed work.'
          : 'Off — takes the work routed to it.')
        + (runner.private ? ' A private runner is always directed.' : '')
      }
      control={
        <Switch
          checked={!!runner.directed}
          onChange={toggle}
          disabled={saving || !!runner.private}
          label="Directed only"
          testId="runner-directed-toggle"
        />
      }
    >
      {err && <p className="text-[12px] text-destructive" data-testid="runner-directed-error">{err}</p>}
    </SettingRow>
  )
}
