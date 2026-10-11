import { useCallback, useEffect, useState, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'

import { getProjectIdeas, type IdeaOut, type ProjectIdeasOut } from '@/api/agents'
import { IdeaRow } from '@/pages/agents/ideaParts'
import { STAGE_LABEL, type Stage } from '@/pages/agents/ideaStage'
import { Section } from '@/pages/agents/sectionLayout'
import { TASK_RESOURCE, useResource } from '@/widget/useResource'

// The project page's PEOPLE + IDEAS (hal T69): who is building this, what each
// of them is after (their contributor page), and every idea with where it
// stands and whose it was. Designed for a few engaged builders: the people
// with pages lead, and their ideas carry their own words.

const ORDER: Stage[] = ['started', 'proposed', 'shipped', 'declined']

export function ProjectIdeasSection({ slug, projectRef }: { slug: string; projectRef: string }): JSX.Element {
  const [state, setState] = useState<{ key: string; data: ProjectIdeasOut | null; error: string | null } | null>(
    null,
  )
  const key = `${slug}/${projectRef}`

  const reload = useCallback(() => {
    let cancelled = false
    getProjectIdeas(slug, projectRef)
      .then((data) => !cancelled && setState({ key, data, error: null }))
      .catch((err: unknown) => {
        if (!cancelled)
          setState({ key, data: null, error: err instanceof Error ? err.message : 'Could not load ideas' })
      })
    return () => {
      cancelled = true
    }
  }, [slug, projectRef, key])

  useEffect(() => reload(), [reload])
  useResource(TASK_RESOURCE, () => {
    reload()
  })

  const fresh = state?.key === key ? state : null
  const data = fresh?.data ?? null
  const { workspace = '' } = useParams<{ workspace: string }>()
  const pageHref = (personId: number) =>
    `/w/${workspace}/agents/${slug}/projects/${encodeURIComponent(projectRef)}/people/${personId}`

  return (
    <>
      <Section
        id="project-people"
        title="People"
        description="Who is building this, and what each of them is after. Read someone's page before you build on their idea — or against it."
      >
        {!fresh ? (
          <p className="text-[13px] text-muted-foreground">Loading…</p>
        ) : !data ? (
          <p className="text-[13px] text-destructive">{fresh.error}</p>
        ) : data.contributors.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">
            Nobody yet. A contributor appears when their conversation raises a task here, or when the agent
            writes their page from their own words.
          </p>
        ) : (
          <ul className="divide-y divide-border rounded-lg border border-border">
            {data.contributors.map((c) => {
              const live = c.ideas.started + c.ideas.proposed
              return (
                <li key={c.id}>
                  <Link
                    to={pageHref(c.id)}
                    data-testid={`contributor-${c.id}`}
                    className="block px-3 py-2 hover:bg-muted"
                  >
                    <div className="flex flex-wrap items-baseline gap-x-3">
                      <span className="text-[13px] font-medium text-foreground">{c.display_name}</span>
                      {c.role && <span className="text-[11px] text-muted-foreground">{c.role}</span>}
                      <span className="ml-auto text-[11px] text-muted-foreground">
                        <span className="text-success">{c.ideas.shipped} shipped</span> · {live} in motion
                      </span>
                    </div>
                    {c.goal ? (
                      <p className="mt-0.5 line-clamp-2 text-[12px] text-foreground-secondary">{c.goal}</p>
                    ) : (
                      <p className="mt-0.5 text-[12px] text-foreground-subtle">
                        {c.page_withheld ? 'Page not shared with agents.' : 'No page written yet.'}
                      </p>
                    )}
                  </Link>
                </li>
              )
            })}
          </ul>
        )}
      </Section>

      <Section
        id="project-ideas"
        title="Ideas"
        description="Every task is an idea: where it stands, whose it was, in their words."
      >
        {data && data.ideas.length > 0 ? (
          <div className="space-y-4">
            <p className="text-[12px] text-muted-foreground" data-testid="idea-counts">
              {ORDER.filter((s) => s !== 'declined')
                .map((s) => `${data.stages[s]} ${STAGE_LABEL[s].toLowerCase()}`)
                .join(' · ')}
            </p>
            {ORDER.map((stage) => {
              const ideas: IdeaOut[] = data.ideas.filter((i) => i.stage === stage)
              if (ideas.length === 0) return null
              const list = (
                <ul className="divide-y divide-border rounded-lg border border-border">
                  {ideas.map((idea) => (
                    <IdeaRow key={idea.ext_id} idea={idea} pageHref={pageHref} />
                  ))}
                </ul>
              )
              // Shipped and not-pursued are history: folded, so what is live leads.
              return stage === 'shipped' || stage === 'declined' ? (
                <details key={stage}>
                  <summary className="cursor-pointer text-[12px] font-medium text-muted-foreground hover:text-foreground">
                    {STAGE_LABEL[stage]} ({ideas.length})
                  </summary>
                  <div className="mt-2">{list}</div>
                </details>
              ) : (
                <div key={stage}>
                  <h3 className="mb-1 text-[12px] font-medium text-muted-foreground">
                    {STAGE_LABEL[stage]} ({ideas.length})
                  </h3>
                  {list}
                </div>
              )
            })}
          </div>
        ) : (
          data && <p className="text-[13px] text-muted-foreground">No ideas on this project yet.</p>
        )}
      </Section>
    </>
  )
}
