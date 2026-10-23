import { useState, type JSX } from 'react'
import { setRunnerDirected, type RunnerOut } from '@/api/harness'
import { SettingRow, Switch } from '@/components/supervisor/SettingRow'

// Directed only: the box takes only turns pinned to it and chats bound to it, no
// routed work. A box can declare it itself (`up.sh --test`); this switch is a
// runner administrator's word over that (`Runner.directed_setting`), so turning it
// off needs no shell on the box. A private runner is always directed.
export function RunnerDirected({
  runner,
  onChange,
}: {
  runner: RunnerOut
  onChange: (fresh: RunnerOut) => void
}): JSX.Element {
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const byBox = (runner.capabilities as { directed?: unknown } | null)?.directed === true
  const overridden = runner.directed_setting !== null && runner.directed_setting !== undefined

  const set = (directed: boolean | null) => {
    if (saving) return
    setSaving(true)
    setErr(null)
    setRunnerDirected(runner.id, directed)
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
        + (runner.private ? ' A private runner is always directed.'
          : overridden ? ` Set here; the box itself says ${byBox ? 'directed' : 'not directed'}.`
            : byBox ? ' The box declares it.' : '')
      }
      control={
        <Switch
          checked={!!runner.directed}
          onChange={() => set(!runner.directed)}
          disabled={saving || !!runner.private}
          label="Directed only"
          testId="runner-directed-toggle"
        />
      }
    >
      {overridden && !runner.private && (
        <button
          type="button"
          onClick={() => set(null)}
          disabled={saving}
          className="self-start text-[12px] text-muted-foreground underline"
          data-testid="runner-directed-follow-box"
        >
          Follow the box's own setting
        </button>
      )}
      {err && <p className="text-[12px] text-destructive" data-testid="runner-directed-error">{err}</p>}
    </SettingRow>
  )
}
