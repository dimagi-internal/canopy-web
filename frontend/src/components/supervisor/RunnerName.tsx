import { useState, type JSX } from 'react'
import { setRunnerName, type RunnerOut } from '@/api/harness'
import { SettingRow } from '@/components/supervisor/SettingRow'

// The runner's name (`Runner.name`): what people see for it everywhere. A runner
// administrator's setting. Routing, pins and the vault follow the runner's id, and
// the box keeps the name it paired as (`paired_as`), so a rename changes nothing else.
export function RunnerName({
  runner,
  onChange,
}: {
  runner: RunnerOut
  onChange: (fresh: RunnerOut) => void
}): JSX.Element {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(runner.name)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const start = () => {
    setValue(runner.name)
    setErr(null)
    setEditing(true)
  }
  const save = () => {
    const name = value.trim()
    if (saving) return
    if (!name) {
      setErr('a name cannot be blank')
      return
    }
    if (name === runner.name) {
      setEditing(false)
      return
    }
    setSaving(true)
    setErr(null)
    setRunnerName(runner.id, name)
      .then((fresh) => {
        setEditing(false)
        onChange(fresh)
      })
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not rename it'))
      .finally(() => setSaving(false))
  }

  const pairedAs = runner.paired_as && runner.paired_as !== runner.name ? `Paired as ${runner.paired_as}.` : ''
  return (
    <SettingRow
      label="Name"
      testId="runner-name"
      help={editing ? 'What people see for this runner. Routing and the vault are unchanged.' : pairedAs || runner.name}
      control={
        editing ? (
          <div className="flex items-center gap-1.5">
            <input
              autoFocus
              value={value}
              disabled={saving}
              maxLength={100}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') save()
                if (e.key === 'Escape') setEditing(false)
              }}
              aria-label="Runner name"
              data-testid="runner-name-input"
              className="w-56 rounded-md border border-input bg-input px-2 py-0.5 text-[12px] text-foreground placeholder:text-muted-foreground"
            />
            <button
              type="button"
              onClick={save}
              disabled={saving}
              data-testid="runner-name-save"
              className="rounded-md bg-primary px-2 py-0.5 text-[12px] text-primary-foreground disabled:opacity-50"
            >
              Save
            </button>
            <button
              type="button"
              onClick={() => setEditing(false)}
              disabled={saving}
              className="rounded-md px-2 py-0.5 text-[12px] text-muted-foreground hover:bg-muted hover:text-foreground"
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={start}
            data-testid="runner-name-edit"
            className="rounded-md border border-border px-2 py-0.5 text-[12px] text-foreground hover:bg-muted"
          >
            Rename
          </button>
        )
      }
    >
      {err && <p className="text-[12px] text-destructive" data-testid="runner-name-error">{err}</p>}
    </SettingRow>
  )
}
