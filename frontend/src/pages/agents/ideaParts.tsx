import { type JSX } from 'react'
import { Link } from 'react-router-dom'

import type { IdeaOut, IdeaSourceOut } from '@/api/agents'
import { saidOn, STAGE_LABEL, type Stage } from '@/pages/agents/ideaStage'

// IDEAS (hal T69). An idea is a board task; its stage is the task's status in
// a contributor's words, and every idea says whose it was — with their words,
// linked to where they said them. Shared by the project page's Ideas section
// and the contributor page.


const STAGE_TONE: Record<Stage, string> = {
  proposed: 'bg-info/10 text-info border-info/30',
  started: 'bg-warning/10 text-warning border-warning/30',
  shipped: 'bg-success/10 text-success border-success/30',
  declined: 'bg-muted text-muted-foreground border-border',
}

export function StagePill({ stage }: { stage: Stage }): JSX.Element {
  return (
    <span
      data-testid={`stage-${stage}`}
      className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-medium ${STAGE_TONE[stage]}`}
    >
      {STAGE_LABEL[stage]}
    </span>
  )
}

const TRACK: Stage[] = ['proposed', 'started', 'shipped']

/** Proposed → Started → Shipped, with how far this idea has got. The point of
 *  the page is watching your idea move along it. */
export function StageTrack({ stage }: { stage: Stage }): JSX.Element {
  if (stage === 'declined') return <StagePill stage={stage} />
  const reached = TRACK.indexOf(stage)
  return (
    <ol className="flex shrink-0 items-center gap-1" aria-label={`Stage: ${STAGE_LABEL[stage]}`}>
      {TRACK.map((s, i) => (
        <li key={s} className="flex items-center gap-1">
          {i > 0 && <span className={`h-px w-3 ${i <= reached ? 'bg-success' : 'bg-border'}`} />}
          <span
            title={STAGE_LABEL[s]}
            className={`h-2 w-2 rounded-full ${
              i < reached ? 'bg-success' : i === reached ? STAGE_DOT[stage] : 'bg-border'
            }`}
          />
        </li>
      ))}
      <li className="ml-1 text-[11px] text-muted-foreground">{STAGE_LABEL[stage]}</li>
    </ol>
  )
}

const STAGE_DOT: Record<Stage, string> = {
  proposed: 'bg-info',
  started: 'bg-warning',
  shipped: 'bg-success',
  declined: 'bg-muted-foreground',
}


/** Their words, quoted, with a link to where they said them. */
export function TheirWords({ source }: { source: IdeaSourceOut }): JSX.Element | null {
  if (!source.quote) return null
  const when = saidOn(source.said_at)
  return (
    <blockquote className="mt-1 border-l-2 border-primary/40 pl-2 text-[12px] italic text-foreground-secondary">
      “{source.quote}”
      {source.source_url && (
        <>
          {' '}
          <a
            href={source.source_url}
            target="_blank"
            rel="noreferrer"
            className="not-italic text-[11px] text-primary hover:underline"
          >
            {when ? `said ${when}` : 'where they said it'}
          </a>
        </>
      )}
    </blockquote>
  )
}

/** "from Jonathan, Sophie" — each a link to that person's contributor page when
 *  canopy knows them. `pageHref` builds the link from a person id. */
export function FromChips({
  sources,
  pageHref,
}: {
  sources: readonly IdeaSourceOut[]
  pageHref: (personId: number) => string
}): JSX.Element {
  if (sources.length === 0) {
    return <span className="text-[11px] text-foreground-subtle">from: not recorded</span>
  }
  return (
    <span className="text-[11px] text-muted-foreground">
      from{' '}
      {sources.map((s, i) => (
        <span key={`${s.person_id ?? s.name}`}>
          {i > 0 && ', '}
          {s.person_id != null ? (
            <Link to={pageHref(s.person_id)} className="text-primary hover:underline">
              {s.name}
            </Link>
          ) : (
            <span className="text-foreground-secondary">{s.name}</span>
          )}
        </span>
      ))}
    </span>
  )
}

/** One idea as a row: stage track, title, whose it is and their words. */
export function IdeaRow({
  idea,
  pageHref,
  showFrom = true,
  words,
}: {
  idea: IdeaOut
  pageHref: (personId: number) => string
  showFrom?: boolean
  /** Whose words to show under the title; defaults to the first source's. */
  words?: IdeaSourceOut | null
}): JSX.Element {
  const quote = words ?? idea.came_from.find((c) => c.quote) ?? null
  return (
    <li data-testid={`idea-${idea.ext_id}`} className="px-3 py-2">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-[11px] text-muted-foreground">{idea.ext_id}</span>
        <span className="min-w-0 flex-1 text-[13px] text-foreground">{idea.title}</span>
        <StageTrack stage={idea.stage} />
      </div>
      {showFrom && (
        <div className="mt-0.5">
          <FromChips sources={idea.came_from} pageHref={pageHref} />
        </div>
      )}
      {quote && <TheirWords source={quote} />}
    </li>
  )
}
