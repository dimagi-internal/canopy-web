import { useEffect, useRef, useState, type JSX } from 'react'
import { setRunnerFlags, type RunnerOut } from '@/api/harness'

// What this box guarantees, declared by the person who runs it.
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
      'This box uses only zero-data-retention keys for Claude. You are vouching for this; canopy cannot check it.',
  },
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
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Guarantees</span>
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
            </span>
          </label>
        )
      })}
      {error && <p className="text-[12px] text-destructive">{error}</p>}
    </div>
  )
}
