import { useState, type JSX } from 'react'
import { setRunnerPrivate, type RunnerOut } from '@/api/harness'

// A runner as its owner's PRIVATE runner (canopy-web#1705). Shared: the box takes
// fleet work as routed. Private: only what its owner pins or binds to it, plus the
// Claude Code sessions opened on that machine (recorded as the owner's local turns —
// the runner writes the registration itself, no CLI). Owner only.
//
// Running as a lower role is not here on purpose: it is a property of one turn
// (`acts_as`), or of a client that puts itself in that mode, never a setting left on.
type Mode = 'shared' | 'private'

const MODE_LABEL: Record<Mode, string> = { shared: 'Shared', private: 'Private' }
const MODE_HELP: Record<Mode, string> = {
  shared: 'Takes fleet work as routed.',
  private: 'Only your own pinned work and the Claude Code sessions on this machine.',
}

export function RunnerPrivate({
  runner,
  onChange,
}: {
  runner: RunnerOut
  onChange: (fresh: RunnerOut) => void
}): JSX.Element {
  const mode: Mode = runner.private ? 'private' : 'shared'
  const [saving, setSaving] = useState<Mode | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const save = (m: Mode) => {
    if (saving) return
    setSaving(m)
    setErr(null)
    setRunnerPrivate(runner.id, m === 'private')
      .then(onChange)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not change it'))
      .finally(() => setSaving(null))
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3" data-testid="runner-private">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Who it works for</span>
        <div className="ml-auto flex flex-wrap gap-1" role="radiogroup" aria-label="Who it works for">
          {(Object.keys(MODE_LABEL) as Mode[]).map((m) => (
            <button
              key={m}
              type="button"
              role="radio"
              aria-checked={m === mode}
              disabled={saving !== null}
              onClick={() => m !== mode && save(m)}
              data-testid={`runner-private-${m}`}
              className={`rounded-md px-2.5 py-1 text-[12px] font-medium disabled:opacity-50 ${
                m === mode ? 'bg-primary text-primary-foreground' : 'border border-border text-foreground hover:bg-muted'
              }`}
            >
              {saving === m ? '…' : MODE_LABEL[m]}
            </button>
          ))}
        </div>
      </div>
      <p className="text-[12px] text-muted-foreground">{MODE_HELP[mode]}</p>
      {err && <p className="text-[12px] text-destructive" data-testid="runner-private-error">{err}</p>}
    </div>
  )
}
