import { useState, type JSX } from 'react'
import { setRunnerEngine, type RunnerEngine as Engine, type RunnerOut } from '@/api/harness'

// The session runtime switch (canopy-web#1188): whether this laptop runner opens
// NEW sessions in emdash or in the Claude desktop app's Code tab. The runner reads
// the setting off its next heartbeat (a few seconds), so there is nothing to
// restart; sessions already running keep going where they are, and routing is
// the same either way. Owner or runner admins, like flags.
const LABEL: Record<Engine, string> = { emdash: 'emdash', 'claude-desktop': 'Claude desktop' }

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
    if (engine === current || saving) return
    setSaving(engine)
    setErr(null)
    setRunnerEngine(runner.id, engine)
      .then(onChange)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not change the runtime'))
      .finally(() => setSaving(null))
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3" data-testid="runner-engine">
      <div className="flex items-center gap-2">
        <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Session runtime</span>
        <div className="ml-auto flex gap-1" role="radiogroup" aria-label="Session runtime">
          {(Object.keys(LABEL) as Engine[]).map((engine) => (
            <button
              key={engine}
              type="button"
              role="radio"
              aria-checked={engine === current}
              disabled={saving !== null}
              onClick={() => choose(engine)}
              data-testid={`runner-engine-${engine}`}
              className={`rounded-md px-2.5 py-1 text-[12px] font-medium disabled:opacity-50 ${
                engine === current
                  ? 'bg-primary text-primary-foreground'
                  : 'border border-border text-foreground hover:bg-muted'
              }`}
            >
              {saving === engine ? '…' : LABEL[engine]}
            </button>
          ))}
        </div>
      </div>
      <p className="text-[12px] text-muted-foreground">
        Where new sessions open. Takes effect within seconds; running sessions stay where they are.
      </p>
      {err && <p className="text-[12px] text-destructive" data-testid="runner-engine-error">{err}</p>}
    </div>
  )
}
