import { useState } from 'react'
import type { DddNarration } from '../../api/ddd'
import { pairNarrationScenes, hasNarrativeChanges, type NarrativeScenePair } from './narrativeScenePairing'

const STATUS_LABEL: Record<NarrativeScenePair['status'], string> = {
  unchanged: 'Unchanged',
  changed: 'Changed',
  added: 'New',
  removed: 'Removed',
}

const STATUS_CLASS: Record<NarrativeScenePair['status'], string> = {
  unchanged: 'bg-muted text-muted-foreground',
  changed: 'bg-warning/15 text-warning',
  added: 'bg-success/15 text-success',
  removed: 'bg-destructive/15 text-destructive',
}

function Cell({ text, muted }: { text: string | null; muted?: boolean }) {
  if (text == null) {
    return <p className="text-xs italic text-muted-foreground">—</p>
  }
  return (
    <p
      className={`whitespace-pre-line text-sm leading-relaxed ${
        muted ? 'text-muted-foreground' : 'text-foreground-secondary'
      }`}
    >
      {text}
    </p>
  )
}

/**
 * A plain-language before/after of two narrative versions' narration, one row per
 * scene, matched by scene id. Read-only — the source of truth stays the versioned
 * narrative on canopy; this is the "three views into the iteration" comparison.
 */
export function NarrativeDiff({
  before,
  after,
  beforeLabel,
  afterLabel,
  defaultOpen = false,
}: {
  before: DddNarration[]
  after: DddNarration[]
  beforeLabel: string
  afterLabel: string
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const pairs = pairNarrationScenes(before, after)
  const changedCount = pairs.filter((p) => p.status !== 'unchanged').length

  if (before.length === 0) {
    return null
  }

  return (
    <div className="mb-3 rounded-lg border border-border bg-background/30">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        <span aria-hidden className="text-muted-foreground">
          {open ? '▾' : '▸'}
        </span>
        <span className="text-xs font-medium text-foreground-secondary">
          Compare {beforeLabel} → {afterLabel}
        </span>
        <span className="ml-auto text-[11px] text-muted-foreground">
          {hasNarrativeChanges(pairs) ? `${changedCount} scene${changedCount === 1 ? '' : 's'} changed` : 'no changes'}
        </span>
      </button>

      {open && (
        <div className="border-t border-border px-3 py-3">
          <div className="mb-2 hidden grid-cols-2 gap-3 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground sm:grid">
            <span>{beforeLabel} (current)</span>
            <span>{afterLabel} (proposed)</span>
          </div>
          <ol className="space-y-2">
            {pairs.map((p, i) => (
              <li
                key={p.id ?? `#${i}`}
                className="rounded-md border border-border/60 bg-background/40 p-2"
              >
                <div className="mb-1 flex items-center gap-2">
                  <span className={`rounded px-1.5 py-0.5 text-[9px] font-medium ${STATUS_CLASS[p.status]}`}>
                    {STATUS_LABEL[p.status]}
                  </span>
                  {p.title && (
                    <span className="truncate text-xs text-foreground-secondary">{p.title}</span>
                  )}
                </div>
                <div className="grid gap-3 sm:grid-cols-2">
                  <Cell text={p.before} muted />
                  <Cell text={p.after} />
                </div>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  )
}
