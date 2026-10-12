import { type JSX } from 'react'
import { Link } from 'react-router-dom'

import type { ContributorBriefOut, IdeaOut } from '@/api/agents'
import { saidOn, STAGE_LABEL, tally, unwritten, type Stage } from '@/pages/agents/ideaStage'

// IDEAS (hal T82). An idea is a task with kind "idea": a person's want about
// the product, in their words, with the work toward it as its subtasks. A task
// of kind "work" — the agent's own — never shows here. Shared by the work
// board's Ideas section, the team view and the contributor page.

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

const STAGE_DOT: Record<Stage, string> = {
  proposed: 'bg-info',
  started: 'bg-warning',
  shipped: 'bg-success',
  declined: 'bg-muted-foreground',
}

/** Proposed → Started → Shipped, with how far this idea has got. */
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

/** Their words, quoted, with a link to where they said them. */
export function TheirWords({ idea }: { idea: IdeaOut }): JSX.Element | null {
  if (!idea.quote) return null
  const when = saidOn(idea.said_at)
  return (
    <blockquote className="mt-1 border-l-2 border-primary/40 pl-2 text-[12px] italic text-foreground-secondary">
      “{idea.quote}”
      {idea.source_url && (
        <>
          {' '}
          <a
            href={idea.source_url}
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

/** One idea as a row: stage, the want, whose it is and their words. `showRefs`
 *  adds the idea's task id and its subtasks — the work board's view; a
 *  teammate sees neither. */
export function IdeaRow({
  idea,
  pageHref,
  showFrom = true,
  showRefs = false,
  control,
}: {
  idea: IdeaOut
  pageHref: (personId: number) => string
  showFrom?: boolean
  showRefs?: boolean
  /** Rendered at the row's end — e.g. the team view's "Mark shipped". */
  control?: JSX.Element
}): JSX.Element {
  const who = idea.who
  return (
    <li data-testid={`idea-${idea.ext_id}`} className="px-3 py-2">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        {showRefs && <span className="text-[11px] text-muted-foreground">{idea.ext_id}</span>}
        <span className="min-w-0 flex-1 text-[13px] text-foreground">
          {idea.title || `“${idea.quote}”`}
        </span>
        <StageTrack stage={idea.stage} />
        {control}
      </div>
      {showFrom && (
        <div className="mt-0.5 text-[11px] text-muted-foreground">
          from{' '}
          {who.person_id != null ? (
            <Link to={pageHref(who.person_id)} className="text-primary hover:underline">
              {who.name}
            </Link>
          ) : (
            <span className="text-foreground-secondary">{who.name}</span>
          )}
          {showRefs && idea.subtasks.length > 0 && (
            <span> · work: {idea.subtasks.map((t) => t.ext_id).join(', ')}</span>
          )}
        </div>
      )}
      {idea.title !== idea.quote && <TheirWords idea={idea} />}
      {idea.title === idea.quote && idea.source_url && (
        <a
          href={idea.source_url}
          target="_blank"
          rel="noreferrer"
          className="mt-0.5 inline-block text-[11px] text-primary hover:underline"
        >
          {saidOn(idea.said_at) ? `said ${saidOn(idea.said_at)}` : 'where they said it'}
        </a>
      )}
    </li>
  )
}

/** The project's people, each tallied over its ideas. */
export function PeopleList({
  people,
  pageHref,
}: {
  people: readonly ContributorBriefOut[]
  pageHref: (personId: number) => string
}): JSX.Element {
  if (people.length === 0) {
    return (
      <p className="text-[13px] text-muted-foreground">
        Nobody yet. Someone appears here when they add an idea, or when their page is written.
      </p>
    )
  }
  return (
    <ul className="divide-y divide-border rounded-lg border border-border">
      {people.map((c) => (
        <li key={c.id}>
          <Link to={pageHref(c.id)} data-testid={`contributor-${c.id}`} className="block px-3 py-2 hover:bg-muted">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
              <span className="text-[13px] font-medium text-foreground">{c.display_name}</span>
              {c.role && <span className="text-[11px] text-muted-foreground">{c.role}</span>}
              <span className="text-[11px] text-muted-foreground sm:ml-auto">{tally(c.ideas)}</span>
            </div>
            {c.goal ? (
              <p className="mt-0.5 line-clamp-2 text-[12px] text-foreground-secondary">{c.goal}</p>
            ) : (
              <p className="mt-0.5 text-[12px] text-foreground-subtle">{unwritten(c.display_name)}</p>
            )}
          </Link>
        </li>
      ))}
    </ul>
  )
}
