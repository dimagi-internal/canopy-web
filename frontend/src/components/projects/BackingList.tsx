import type { JSX } from 'react'

import type { BackingStance, ProjectBackingOut } from '@/api/agents'

import { STANCE_WORDS } from './stance'

// WHERE EVERYONE STANDS on a project, one short line each:
// "Echo backs · Eva backs with changes: … · Shayoni passes: …". A row from an
// earlier version of the project (it was revised since) says so, because a
// "backs" given to a different plan is not a "backs" for this one.

const STANCE_TONE: Record<BackingStance, string> = {
  backs: 'text-success',
  backs_with_changes: 'text-warning',
  passes: 'text-muted-foreground',
}

export function BackingList({ backings }: { backings: readonly ProjectBackingOut[] }): JSX.Element {
  if (backings.length === 0) {
    return <p className="text-[12px] text-muted-foreground">Nobody has backed or passed on it yet.</p>
  }
  return (
    <ul aria-label="Backings" className="space-y-1 text-[13px]">
      {backings.map((b) => (
        <li
          key={`${b.backer.kind}-${b.backer.agent || b.backer.person_id}`}
          data-backing={b.stance}
          className={'leading-snug ' + (b.stale ? 'opacity-60' : '')}
        >
          <span className="font-medium text-foreground">{b.mine ? 'You' : b.backer.name}</span>{' '}
          <span className={STANCE_TONE[b.stance]}>{STANCE_WORDS[b.stance]}</span>
          {b.line && <span className="text-foreground-secondary">: {b.line}</span>}
          {b.stale && <span className="ml-1 text-[11px] text-muted-foreground">(an earlier version)</span>}
        </li>
      ))}
    </ul>
  )
}
