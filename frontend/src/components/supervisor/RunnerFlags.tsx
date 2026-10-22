import { useEffect, useRef, useState, type JSX } from 'react'
import { setRunnerFlags, type RunnerOut } from '@/api/harness'

// What this box's owner declares about it. Headed by WHO is speaking, not
// "Guarantees": canopy cannot check a declaration, so it guarantees nothing.
//
// The list of declarable flags comes from the server (`known_flags`); this file
// keeps only the WORDING for each, so a flag the server learns before the UI does
// still renders (with a generic sentence) instead of vanishing.
//
// canopy cannot inspect a box's Claude keys, so every flag is a promise, and the
// sentence saying so sits beside the checkbox rather than in a help page.
const WORDS: Record<string, { label: string; sentence: string }> = {
  zdr: {
    label: 'ZDR',
    sentence:
      'This box runs in ZDR mode: one zero-data-retention key for Claude and a locked profile on every turn. You are vouching for this; canopy cannot check the key.',
  },
}

// A flag that names a data mode counts only while the box itself reports that mode
// on its heartbeat (apps/harness/runner_requirements.py `FLAG_NEEDS_DATA_MODE`):
// a declared ZDR on a box that is not running in ZDR mode routes no ZDR work.
const NEEDS_DATA_MODE: Record<string, string> = { zdr: 'zdr' }

function notInForce(runner: RunnerOut, flag: string): boolean {
  const mode = NEEDS_DATA_MODE[flag]
  if (!mode || !runner.flags.includes(flag)) return false
  const caps = (runner.capabilities ?? {}) as Record<string, unknown>
  return caps.data_mode !== mode
}

function wordsFor(flag: string): { label: string; sentence: string } {
  return WORDS[flag] ?? { label: flag.toUpperCase(), sentence: 'You are vouching for this; canopy cannot check it.' }
}

export function RunnerFlags({
  runner,
  onChange,
}: {
  runner: RunnerOut
  onChange: (runner: RunnerOut) => void
}): JSX.Element {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])

  const toggle = (flag: string) => {
    const has = runner.flags.includes(flag)
    const next = has ? runner.flags.filter((f) => f !== flag) : [...runner.flags, flag]
    setBusy(true)
    setError(null)
    setRunnerFlags(runner.id, next)
      .then((updated) => onChange(updated))
      // The server's own words (an unknown flag is a 422 that names it).
      .catch((e: unknown) => { if (alive.current) setError(e instanceof Error ? e.message : 'Failed to update') })
      .finally(() => { if (alive.current) setBusy(false) })
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3" data-testid="runner-flags">
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Declared by the owner</span>
      {runner.known_flags.map((flag) => {
        const { label, sentence } = wordsFor(flag)
        return (
          <label key={flag} className="flex items-start gap-2 text-[12px]">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={runner.flags.includes(flag)}
              disabled={busy}
              onChange={() => toggle(flag)}
              aria-label={label}
            />
            <span className="flex flex-col">
              <span className="font-medium text-foreground">{label}</span>
              <span className="text-muted-foreground">{sentence}</span>
              {notInForce(runner, flag) && (
                <span className="text-warning" data-testid={`runner-flag-not-in-force-${flag}`}>
                  Not in force: this box is not reporting ZDR mode, so it takes no ZDR work.
                </span>
              )}
            </span>
          </label>
        )
      })}
      {error && <p className="text-[12px] text-destructive">{error}</p>}
    </div>
  )
}
