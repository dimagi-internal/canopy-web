import { useState, type JSX } from 'react'

import {
  AgentApiError,
  backProject,
  unbackProject,
  type BackingStance,
  type ProjectBackingOut,
  type ProposedProjectOut,
} from '@/api/agents'

import { STANCE_WORDS } from './stance'

// SAY WHERE YOU STAND — any member, as themself. Backing is a signal, never a
// gate: it starts nothing (that is the decision card's). "Back it" is one
// click; "with changes" and "pass" take one line saying what or why, because a
// bare "no" teaches the agent nothing.

const CHOICES: { stance: BackingStance; label: string }[] = [
  { stance: 'backs', label: 'Back it' },
  { stance: 'backs_with_changes', label: 'Back with changes' },
  { stance: 'passes', label: 'Pass' },
]

export function BackingControl({
  project,
  mine,
  onChanged,
}: {
  project: Pick<ProposedProjectOut, 'agent' | 'workspace' | 'ref'>
  /** The reader's own backing, if they gave one. */
  mine: ProjectBackingOut | undefined
  onChanged: () => void
}): JSX.Element {
  const [picked, setPicked] = useState<BackingStance | null>(null)
  const [line, setLine] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = (work: Promise<unknown>) => {
    setBusy(true)
    setError(null)
    void work
      .then(() => {
        setPicked(null)
        setLine('')
        onChanged()
      })
      .catch((err: unknown) =>
        setError(err instanceof AgentApiError && err.detail ? err.detail : 'Could not save that'),
      )
      .finally(() => setBusy(false))
  }
  const send = (stance: BackingStance) => run(backProject(project, stance, line.trim()))

  return (
    <div className="space-y-2" data-backing-control>
      <div className="flex flex-wrap items-center gap-1.5">
        {mine && !mine.stale && (
          <span className="mr-1 text-[12px] text-muted-foreground">You {STANCE_WORDS[mine.stance]}.</span>
        )}
        {CHOICES.map((c) => (
          <button
            key={c.stance}
            type="button"
            disabled={busy}
            aria-pressed={picked === c.stance}
            onClick={() => (c.stance === 'backs' ? send('backs') : setPicked(c.stance))}
            className={
              'min-h-11 rounded border px-2 py-1 text-[12px] disabled:opacity-50 sm:min-h-0 ' +
              (picked === c.stance
                ? 'border-primary bg-primary/10 text-foreground'
                : 'border-border text-foreground-secondary hover:bg-muted')
            }
          >
            {c.label}
          </button>
        ))}
        {mine && (
          <button
            type="button"
            disabled={busy}
            onClick={() => run(unbackProject(project))}
            className="min-h-11 px-1 text-[12px] text-muted-foreground hover:text-foreground disabled:opacity-50 sm:min-h-0"
          >
            Withdraw
          </button>
        )}
      </div>
      {picked && picked !== 'backs' && (
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            if (line.trim()) send(picked)
          }}
        >
          <input
            aria-label={picked === 'passes' ? 'Why you pass' : 'What you would change'}
            value={line}
            maxLength={280}
            onChange={(e) => setLine(e.target.value)}
            placeholder={picked === 'passes' ? 'Why you pass, in one line' : 'What you would change, in one line'}
            className="min-h-11 flex-1 rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground sm:min-h-0 sm:min-w-64"
          />
          <button
            type="submit"
            disabled={busy || !line.trim()}
            className="min-h-11 rounded bg-primary px-2 py-1 text-[12px] text-primary-foreground hover:bg-primary/90 disabled:opacity-50 sm:min-h-0"
          >
            Save
          </button>
        </form>
      )}
      {error && <p className="text-[12px] text-destructive">{error}</p>}
    </div>
  )
}
