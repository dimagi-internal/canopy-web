import { useState, type JSX } from 'react'
import { setRunnerPrivate, type RunnerActsAs, type RunnerOut } from '@/api/harness'

// A runner as its owner's PRIVATE runner (canopy-web#1705). Shared: the box takes
// fleet work as routed. Private: only what its owner pins or binds to it, plus the
// Claude Code sessions opened on that machine (recorded as the owner's local turns —
// the runner writes the registration itself, no CLI). "Act as" lowers the role
// everything from the box is judged as, so an owner can test what an editor or a
// viewer gets — licenses, manual-only turns, refusals — on their own Mac. Owner only.
type Choice = 'shared' | 'self' | 'editor' | 'viewer'

const LABEL: Record<Choice, string> = {
  shared: 'Shared',
  self: 'Private · me',
  editor: 'Private · as editor',
  viewer: 'Private · as viewer',
}

const HELP: Record<Choice, string> = {
  shared: 'Takes fleet work as routed.',
  self: 'Only your own pinned work and the sessions on this machine, as yourself.',
  editor: 'Only your own work, judged as a workspace editor: licensed, always manual.',
  viewer: 'Only your own work, judged as a viewer: most agents refuse it.',
}

function choiceOf(runner: RunnerOut): Choice {
  if (!runner.private) return 'shared'
  if (runner.acts_as === 'editor' || runner.acts_as === 'viewer') return runner.acts_as
  return 'self'
}

export function RunnerPrivate({
  runner,
  onChange,
}: {
  runner: RunnerOut
  onChange: (fresh: RunnerOut) => void
}): JSX.Element {
  const current = choiceOf(runner)
  const [saving, setSaving] = useState<Choice | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const choose = (choice: Choice) => {
    if (choice === current || saving) return
    setSaving(choice)
    setErr(null)
    const actsAs: RunnerActsAs = choice === 'editor' || choice === 'viewer' ? choice : ''
    setRunnerPrivate(runner.id, choice !== 'shared', actsAs)
      .then(onChange)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not change it'))
      .finally(() => setSaving(null))
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3" data-testid="runner-private">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Private runner</span>
        <div className="ml-auto flex flex-wrap gap-1" role="radiogroup" aria-label="Private runner">
          {(Object.keys(LABEL) as Choice[]).map((choice) => (
            <button
              key={choice}
              type="button"
              role="radio"
              aria-checked={choice === current}
              disabled={saving !== null}
              onClick={() => choose(choice)}
              data-testid={`runner-private-${choice}`}
              className={`rounded-md px-2.5 py-1 text-[12px] font-medium disabled:opacity-50 ${
                choice === current
                  ? 'bg-primary text-primary-foreground'
                  : 'border border-border text-foreground hover:bg-muted'
              }`}
            >
              {saving === choice ? '…' : LABEL[choice]}
            </button>
          ))}
        </div>
      </div>
      <p className="text-[12px] text-muted-foreground">{HELP[current]}</p>
      {err && <p className="text-[12px] text-destructive" data-testid="runner-private-error">{err}</p>}
    </div>
  )
}
