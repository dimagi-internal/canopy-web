import { useState, type JSX } from 'react'
import { setRunnerPrivate, type RunnerActsAs, type RunnerOut } from '@/api/harness'

// A runner as its owner's PRIVATE runner (canopy-web#1705). Shared: the box takes
// fleet work as routed. Private: only what its owner pins or binds to it, plus the
// Claude Code sessions opened on that machine (recorded as the owner's local turns —
// the runner writes the registration itself, no CLI). Owner only.
//
// "Test as" is a testing tool, not a kind of runner, so it sits folded away under
// a private runner: it lowers the role everything from the box is judged as, so an
// owner can see what an editor or a viewer gets — licenses, manual-only turns,
// refusals — on their own Mac. While it is on, the panel says so, loudly.
type Mode = 'shared' | 'private'
type TestRole = '' | 'editor' | 'viewer'

const MODE_LABEL: Record<Mode, string> = { shared: 'Shared', private: 'Private' }
const MODE_HELP: Record<Mode, string> = {
  shared: 'Takes fleet work as routed.',
  private: 'Only your own pinned work and the Claude Code sessions on this machine.',
}
const TEST_LABEL: Record<TestRole, string> = { '': 'Off', editor: 'Editor', viewer: 'Viewer' }
const TEST_HELP: Record<Exclude<TestRole, ''>, string> = {
  editor: 'an editor: agents run under a license, always in manual mode',
  viewer: 'a viewer: most agents refuse it',
}

export function RunnerPrivate({
  runner,
  onChange,
}: {
  runner: RunnerOut
  onChange: (fresh: RunnerOut) => void
}): JSX.Element {
  const mode: Mode = runner.private ? 'private' : 'shared'
  const testRole: TestRole = runner.private && (runner.acts_as === 'editor' || runner.acts_as === 'viewer')
    ? runner.acts_as : ''
  const [saving, setSaving] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const save = (key: string, priv: boolean, actsAs: RunnerActsAs) => {
    if (saving) return
    setSaving(key)
    setErr(null)
    setRunnerPrivate(runner.id, priv, actsAs)
      .then(onChange)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not change it'))
      .finally(() => setSaving(null))
  }

  const pill = (active: boolean) =>
    `rounded-md px-2.5 py-1 text-[12px] font-medium disabled:opacity-50 ${
      active ? 'bg-primary text-primary-foreground' : 'border border-border text-foreground hover:bg-muted'
    }`

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
              onClick={() => m !== mode && save(m, m === 'private', '')}
              data-testid={`runner-private-${m}`}
              className={pill(m === mode)}
            >
              {saving === m ? '…' : MODE_LABEL[m]}
            </button>
          ))}
        </div>
      </div>
      <p className="text-[12px] text-muted-foreground">{MODE_HELP[mode]}</p>
      {testRole && (
        <p className="rounded-md bg-warning/15 px-2 py-1 text-[12px] text-warning" data-testid="runner-test-role-on">
          Testing: everything from this machine is judged as {TEST_HELP[testRole]}. Turn it off when you are done.
        </p>
      )}
      {mode === 'private' && (
        <details open={testRole !== ''} className="text-[12px]">
          <summary className="cursor-pointer text-muted-foreground">Test as a lower role</summary>
          <div className="mt-2 flex flex-wrap items-center gap-1" role="radiogroup" aria-label="Test as a lower role">
            {(Object.keys(TEST_LABEL) as TestRole[]).map((r) => (
              <button
                key={r || 'off'}
                type="button"
                role="radio"
                aria-checked={r === testRole}
                disabled={saving !== null}
                onClick={() => r !== testRole && save(`test-${r}`, true, r)}
                data-testid={`runner-test-${r || 'off'}`}
                className={pill(r === testRole)}
              >
                {saving === `test-${r}` ? '…' : TEST_LABEL[r]}
              </button>
            ))}
          </div>
          <p className="mt-1 text-muted-foreground">
            See what a teammate with that role gets from your agents, without a second account.
          </p>
        </details>
      )}
      {err && <p className="text-[12px] text-destructive" data-testid="runner-private-error">{err}</p>}
    </div>
  )
}
