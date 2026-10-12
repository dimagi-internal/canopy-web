import { useCallback, useEffect, useState, type JSX } from 'react'
import { useParams } from 'react-router-dom'

import { getProjectIdeas, type IdeaOut, type ProjectIdeasOut } from '@/api/agents'
import { IdeaRow, PeopleList } from '@/pages/agents/ideaParts'
import { STAGE_LABEL, tally, type Stage } from '@/pages/agents/ideaStage'
import { Section } from '@/pages/agents/sectionLayout'
import { TASK_RESOURCE, useResource } from '@/widget/useResource'

// The work board's PEOPLE + IDEAS (hal T69, T82): who is building this and
// what each of them is after, and the ideas — tasks of kind "idea", a person's
// want in their words — with the subtasks that work on each. An idea ships
// when its task is marked done (on its card below). The team view shows the
// same list without the task ids.

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
        ) : (
          <PeopleList people={data.contributors} pageHref={pageHref} />
        )}
      </Section>

      <Section
        id="project-ideas"
        title="Ideas"
        description="People's wants for the product, in their words, and the work on each (its subtasks). The agent's own work is not an idea and is not listed here."
      >
        {data && data.ideas.length > 0 ? (
          <div className="space-y-4">
            <p className="text-[12px] text-muted-foreground" data-testid="idea-counts">
              {tally(data.stages)}
            </p>
            {ORDER.map((stage) => {
              const ideas: IdeaOut[] = data.ideas.filter((i) => i.stage === stage)
              if (ideas.length === 0) return null
              const list = (
                <ul className="divide-y divide-border rounded-lg border border-border">
                  {ideas.map((idea) => (
                    <IdeaRow key={idea.ext_id} idea={idea} pageHref={pageHref} showRefs />
                  ))}
                </ul>
              )
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
