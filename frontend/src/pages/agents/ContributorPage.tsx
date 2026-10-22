import { useCallback, useEffect, useState, type JSX } from 'react'
import { Link, useOutletContext, useParams } from 'react-router-dom'

import { AgentApiError, getContributorPage, type ContributorPageOut, type IdeaOut } from '@/api/agents'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { IdeaRow } from '@/pages/agents/ideaParts'
import { saidOn, tally, unwritten, type Stage } from '@/pages/agents/ideaStage'
import { Section } from '@/pages/agents/sectionLayout'
import { TASK_RESOURCE, useResource } from '@/widget/useResource'
import { WorkbenchSkeleton } from 'canopy-ui'
import { scopedPath } from '../../lib/scopedLinks'

// ONE CONTRIBUTOR on one project (hal T69): what they are trying to achieve, in
// their own words with links to where they said it, and their ideas — built so
// far versus still open. An agent wrote the summary from their sessions; this
// page only renders it. Read it before building on someone's idea, or against it.
// Task numbers show only to whoever runs the work; a teammate gets no ids.

const GROUPS: { stage: Stage; title: string; empty?: string }[] = [
  { stage: 'shipped', title: 'Built so far' },
  { stage: 'started', title: 'In progress' },
  { stage: 'proposed', title: 'Proposed, not started' },
  { stage: 'declined', title: 'Considered, not pursued' },
]

export function ContributorPage(): JSX.Element {
  const { agent, canEdit } = useOutletContext<AgentOutletContext>()
  const { workspace = '', ref = '', person = '' } = useParams<{ workspace: string; ref: string; person: string }>()
  const base = scopedPath(workspace, `/agents/${agent.slug}/projects/${encodeURIComponent(ref)}`)
  return <ContributorPageView slug={agent.slug} projectRef={ref} person={person} base={base} showRefs={canEdit} />
}

function ContributorPageView({
  slug,
  projectRef: ref,
  person,
  base,
  showRefs,
}: {
  slug: string
  projectRef: string
  person: string
  /** The project page this person page sits under; siblings are `${base}/people/<id>`. */
  base: string
  showRefs: boolean
}): JSX.Element {
  const agent = { slug }
  const key = `${agent.slug}/${ref}/${person}`
  const [state, setState] = useState<{ key: string; page: ContributorPageOut | null; error: string | null } | null>(
    null,
  )

  const reload = useCallback(() => {
    let cancelled = false
    getContributorPage(agent.slug, ref, person)
      .then((page) => !cancelled && setState({ key, page, error: null }))
      .catch((err: unknown) => {
        if (cancelled) return
        const error =
          err instanceof AgentApiError && err.status === 404
            ? `No contributor ${person} on ${ref}.`
            : err instanceof Error
              ? err.message
              : 'Could not load the page'
        setState({ key, page: null, error })
      })
    return () => {
      cancelled = true
    }
  }, [agent.slug, ref, person, key])

  useEffect(() => reload(), [reload])
  useResource(TASK_RESOURCE, () => {
    reload()
  })

  const back = (
    <Link to={base} className="text-[12px] text-muted-foreground hover:text-foreground">
      ← Back to the project
    </Link>
  )
  const fresh = state?.key === key ? state : null
  if (!fresh) {
    return (
      <div className="max-w-4xl px-6 py-8">
        {back}
        <WorkbenchSkeleton />
      </div>
    )
  }
  const page = fresh.page
  if (!page) {
    return (
      <div className="max-w-4xl px-6 py-8">
        {back}
        <p className="mt-4 text-[13px] text-destructive">{fresh.error}</p>
      </div>
    )
  }

  // Sibling contributor pages, from inside this one.
  const pageHref = (personId: number) => `${base}/people/${personId}`
  const summary = page.summary
  const total = page.stages.shipped + page.stages.started + page.stages.proposed

  return (
    <div className="max-w-4xl px-6 py-8">
      <Link to={base} className="text-[12px] text-muted-foreground hover:text-foreground">
        ← {showRefs ? `${page.project} ` : ''}
        {page.project_name}
      </Link>

      <header className="mt-3 mb-8 border-b border-border pb-4">
        <div className="flex flex-wrap items-baseline gap-2">
          <h1 className="text-base font-semibold text-foreground">{page.person.display_name}</h1>
          {page.role && <span className="text-[12px] text-muted-foreground">{page.role}</span>}
          <span className="text-[12px] text-muted-foreground">on {page.project_name}</span>
        </div>
        {total > 0 && (
          <p className="mt-2 text-[13px] text-foreground-secondary" data-testid="contributor-tally">
            {tally(page.stages)}
          </p>
        )}
      </header>

      <Section id="contributor-goal" title="What they're trying to achieve">
        {summary ? (
          <div>
            <p className="text-[14px] leading-relaxed text-foreground" data-testid="contributor-goal">
              {summary.goal}
            </p>
            <h3 className="mt-5 mb-2 text-[12px] font-medium text-muted-foreground">In their own words</h3>
            <ul className="space-y-3">
              {summary.quotes.map((q) => (
                <li key={`${q.source_url}|${q.quote}`}>
                  <blockquote className="border-l-2 border-primary/50 pl-3 text-[13px] italic text-foreground-secondary">
                    “{q.quote}”
                  </blockquote>
                  <a
                    href={q.source_url}
                    target="_blank"
                    rel="noreferrer"
                    className="ml-3.5 text-[11px] text-primary hover:underline"
                  >
                    {q.where || 'Where they said it'}
                    {q.said_at && !q.where ? `, ${saidOn(q.said_at)}` : ''}
                  </a>
                </li>
              ))}
            </ul>
          </div>
        ) : page.summary_state === 'withheld' ? (
          <p className="text-[13px] text-muted-foreground">
            {page.person.display_name} has not let agents use what they have learned about them, so this page is
            not shown to agents.
          </p>
        ) : (
          <p className="text-[13px] text-muted-foreground" data-testid="contributor-unwritten">
            {unwritten(page.person.display_name)}
          </p>
        )}
      </Section>

      <Section id="contributor-ideas" title="Their ideas" description="Built so far versus still open.">
        {page.ideas.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">No ideas from them on this project yet.</p>
        ) : (
          <div className="space-y-4">
            {GROUPS.map(({ stage, title }) => {
              const ideas: IdeaOut[] = page.ideas.filter((i) => i.stage === stage)
              if (ideas.length === 0) return null
              return (
                <div key={stage}>
                  <h3 className="mb-1 text-[12px] font-medium text-muted-foreground">
                    {title} ({ideas.length})
                  </h3>
                  <ul className="divide-y divide-border rounded-lg border border-border">
                    {ideas.map((idea) => (
                      <IdeaRow
                        key={idea.ext_id}
                        idea={idea}
                        pageHref={pageHref}
                        // It is their page: every idea here is theirs.
                        showFrom={false}
                        showRefs={showRefs}
                      />
                    ))}
                  </ul>
                </div>
              )
            })}
          </div>
        )}
        {summary && summary.open.length > 0 && (
          <div className="mt-4">
            <h3 className="mb-1 text-[12px] font-medium text-muted-foreground">
              Also wants, not yet added as ideas ({summary.open.length})
            </h3>
            <ul className="divide-y divide-border rounded-lg border border-border" data-testid="contributor-open">
              {summary.open.map((o) => (
                <li key={o.text} className="px-3 py-2">
                  <span className="text-[13px] text-foreground">{o.text}</span>
                  {o.quote && (
                    <blockquote className="mt-1 border-l-2 border-primary/40 pl-2 text-[12px] italic text-foreground-secondary">
                      “{o.quote}”
                      {o.source_url && (
                        <>
                          {' '}
                          <a
                            href={o.source_url}
                            target="_blank"
                            rel="noreferrer"
                            className="not-italic text-[11px] text-primary hover:underline"
                          >
                            {o.said_at ? `said ${saidOn(o.said_at)}` : 'where they said it'}
                          </a>
                        </>
                      )}
                    </blockquote>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </Section>

      {summary && (
        <Section id="contributor-sources" title="How this page was written">
          <p className="text-[12px] text-muted-foreground">
            Written by {summary.written_by || 'an agent'}
            {summary.written_at ? ` on ${saidOn(summary.written_at)}` : ''}
            {summary.basis ? `, from ${summary.basis}` : ''}. The goal is the agent's summary; everything quoted
            is their own words. Where you see it differently, say so — your experience is the better evidence.
          </p>
          {summary.sources.length > 0 && (
            <ul className="mt-2 space-y-1 text-[12px]">
              {summary.sources.map((s) => (
                <li key={s.url}>
                  <a href={s.url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
                    {s.label}
                  </a>
                </li>
              ))}
            </ul>
          )}
        </Section>
      )}
    </div>
  )
}
