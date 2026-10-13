import { useCallback, useEffect, useState, type FormEvent, type JSX, type ReactNode } from 'react'

import { actOnTask, addIdea, AgentApiError, getProjectTeam, type IdeaOut, type ProjectTeamOut } from '@/api/agents'
import { IdeaRow, PeopleList, StagePill } from '@/pages/agents/ideaParts'
import { STAGE_LABEL, tally, type Stage } from '@/pages/agents/ideaStage'
import { Section } from '@/pages/agents/sectionLayout'
import { TASK_RESOURCE, useResource } from '@/widget/useResource'
import { WorkbenchSkeleton } from 'canopy-ui'

// A PROJECT'S OVERVIEW — the top of its one page, and all a teammate needs:
// what it is, where it stands, the ideas (with "Add your idea"), the people.
// No task cards, approve / decline / nudge, rationale or review logs here; the
// agent's work on the project sits below it on the same page (AgentProjectPage).
// There used to be a separate "team view" address carrying this alone; it now
// redirects to the project page, so there is one place to go and one link to share.

const ORDER: Stage[] = ['started', 'proposed', 'shipped', 'declined']

export function ProjectOverview({
  agent,
  projectRef: ref,
  pageHref,
  showRefs = false,
  details,
}: {
  agent: string
  projectRef: string
  /** A contributor's page on this project. */
  pageHref: (personId: number) => string
  /** Task numbers on the ideas, for whoever runs the work. */
  showRefs?: boolean
  /** The project's own fields under the header (status, Drive, repo). */
  details?: ReactNode
}): JSX.Element {
  const key = `${agent}/${ref}`
  const [state, setState] = useState<{
    key: string
    data: ProjectTeamOut | null
    error: string | null
  } | null>(null)

  const reload = useCallback(() => {
    let cancelled = false
    getProjectTeam(agent, ref)
      .then((data) => !cancelled && setState({ key, data, error: null }))
      .catch((err: unknown) => {
        if (cancelled) return
        const error =
          err instanceof AgentApiError && err.status === 404
            ? 'This project does not exist, or you are not a member of its workspace.'
            : err instanceof Error
              ? err.message
              : 'Could not load the project'
        setState({ key, data: null, error })
      })
    return () => {
      cancelled = true
    }
  }, [agent, ref, key])

  useEffect(() => reload(), [reload])
  // An idea is a task: one moving (here, on its card below, or by the agent) moves this.
  useResource(TASK_RESOURCE, () => {
    reload()
  })

  const fresh = state?.key === key ? state : null
  if (!fresh) return <WorkbenchSkeleton />
  const data = fresh.data
  if (!data) return <p className="text-[13px] text-destructive">{fresh.error}</p>

  return (
    <div data-testid="project-overview">
      {/* What it is */}
      <header className="mb-8 border-b border-border pb-5">
        <p className="text-[11px] uppercase tracking-wide text-muted-foreground">Project</p>
        <h1 className="mt-1 text-xl font-semibold text-foreground">{data.name}</h1>
        {data.outcome && (
          <p className="mt-2 text-[14px] leading-relaxed text-foreground-secondary" data-testid="team-outcome">
            {data.outcome}
          </p>
        )}
        {data.links.length > 0 && (
          <ul className="mt-3 flex flex-wrap gap-2" data-testid="team-links">
            {data.links.map((l) => (
              <li key={`${l.label}|${l.url}`}>
                <a
                  href={l.url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-block rounded-full border border-border px-2.5 py-1 text-[12px] text-primary hover:bg-muted"
                >
                  {l.label || l.url}
                </a>
              </li>
            ))}
          </ul>
        )}
        <p className="mt-3 text-[12px] text-muted-foreground">
          {data.owner_name
            ? `Owned by ${data.owner_name}, built with ${data.agent_name}.`
            : `Built with ${data.agent_name}.`}
        </p>
        {details}
      </header>

      {/* Where it stands */}
      <Section id="team-standing" title="Where it stands">
        <div className="space-y-5">
          <StandingList title="Built" ideas={data.built} empty="Nothing is marked shipped yet." />
          {data.in_progress.length > 0 && <StandingList title="In progress" ideas={data.in_progress} />}
          <div>
            <h3 className="mb-1 text-[12px] font-medium text-muted-foreground">
              Waiting on a decision ({data.waiting.length})
            </h3>
            {data.waiting.length === 0 ? (
              <p className="text-[13px] text-muted-foreground">Nothing is waiting on anyone.</p>
            ) : (
              <ul className="divide-y divide-border rounded-lg border border-border" data-testid="team-decisions">
                {data.waiting.map((d) => (
                  <li key={`${d.text}|${d.url}`} className="flex flex-wrap items-baseline gap-x-2 px-3 py-2">
                    <span className="min-w-0 flex-1 text-[13px] text-foreground">
                      {d.url ? (
                        <a href={d.url} target="_blank" rel="noreferrer" className="hover:underline">
                          {d.text}
                        </a>
                      ) : (
                        d.text
                      )}
                    </span>
                    <span className="text-[11px] text-muted-foreground">waiting on {d.waiting_on || 'someone'}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </Section>

      {/* Ideas */}
      <Section
        id="team-ideas"
        title="Ideas"
        description="What people want this to do, in their own words, and where each one stands."
      >
        <AddIdea agent={agent} projectRef={ref} projectName={data.name} onAdded={reload} />
        {data.ideas.length === 0 ? (
          <p className="mt-4 text-[13px] text-muted-foreground">No ideas yet — yours could be the first.</p>
        ) : (
          <div className="mt-5 space-y-4">
            <p className="text-[12px] text-muted-foreground" data-testid="idea-counts">
              {tally(data.stages)}
            </p>
            {ORDER.map((stage) => {
              const ideas: IdeaOut[] = data.ideas.filter((i) => i.stage === stage)
              if (ideas.length === 0) return null
              return (
                <div key={stage}>
                  <h3 className="mb-1 text-[12px] font-medium text-muted-foreground">
                    {STAGE_LABEL[stage]} ({ideas.length})
                  </h3>
                  <ul className="divide-y divide-border rounded-lg border border-border">
                    {ideas.map((idea) => (
                      <IdeaRow
                        key={idea.ext_id}
                        idea={idea}
                        pageHref={pageHref}
                        showRefs={showRefs}
                        control={
                          // Its person, or whoever runs the work, calls it shipped.
                          (stage === 'proposed' || stage === 'started') &&
                          (data.may_edit || (data.my_person_id != null && idea.people.includes(data.my_person_id))) ? (
                            <MarkShipped agent={agent} idea={idea} onDone={reload} />
                          ) : undefined
                        }
                      />
                    ))}
                  </ul>
                </div>
              )
            })}
          </div>
        )}
      </Section>

      {/* People */}
      <Section id="team-people" title="People" description="Who is behind these ideas, and what each of them is after.">
        <PeopleList people={data.contributors} pageHref={pageHref} />
      </Section>
    </div>
  )
}

function MarkShipped({ agent, idea, onDone }: { agent: string; idea: IdeaOut; onDone: () => void }): JSX.Element {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  return (
    <span className="inline-flex items-center gap-1">
      <button
        type="button"
        disabled={busy}
        onClick={() => {
          setBusy(true)
          setError(null)
          actOnTask(agent, idea.ext_id, 'done')
            .then(() => onDone())
            .catch((err: unknown) => setError(err instanceof Error ? err.message : 'Could not mark it shipped'))
            .finally(() => setBusy(false))
        }}
        className="rounded border border-border px-1.5 py-0.5 text-[11px] text-foreground hover:bg-muted disabled:opacity-50"
        data-testid="mark-shipped"
      >
        Mark shipped
      </button>
      {error && <span className="text-[11px] text-destructive">{error}</span>}
    </span>
  )
}

function StandingList({
  title,
  ideas,
  empty,
}: {
  title: string
  ideas: readonly IdeaOut[]
  empty?: string
}): JSX.Element {
  return (
    <div>
      <h3 className="mb-1 text-[12px] font-medium text-muted-foreground">{title}</h3>
      {ideas.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">{empty}</p>
      ) : (
        <ul className="space-y-1">
          {ideas.map((i) => (
            <li key={i.ext_id} className="flex items-baseline gap-2 text-[13px] text-foreground">
              <StagePill stage={i.stage} />
              <span className="min-w-0">{i.title || i.quote}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function AddIdea({
  agent,
  projectRef,
  projectName,
  onAdded,
}: {
  agent: string
  projectRef: string
  projectName: string
  onAdded: () => void
}): JSX.Element {
  const [open, setOpen] = useState(false)
  const [quote, setQuote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  const submit = (e: FormEvent) => {
    e.preventDefault()
    const words = quote.trim()
    if (!words || busy) return
    setBusy(true)
    setError(null)
    addIdea(agent, projectRef, { quote: words })
      .then((out) => {
        const told = out.notified.length > 0 ? `We told ${out.notified.join(' and ')}. ` : ''
        setDone(
          `Added — it is on the list below as Proposed, in your words. ${told}When someone starts work on it, its status here changes to Started.`,
        )
        setQuote('')
        setOpen(false)
        onAdded()
      })
      .catch((err: unknown) =>
        setError(
          err instanceof AgentApiError && err.detail
            ? err.detail
            : err instanceof Error
              ? err.message
              : 'Could not add your idea',
        ),
      )
      .finally(() => setBusy(false))
  }

  return (
    <div>
      {done && (
        <p
          role="status"
          className="mb-3 rounded-md border border-success/30 bg-success/10 px-3 py-2 text-[13px] text-foreground"
          data-testid="idea-added"
        >
          {done}
        </p>
      )}
      {!open ? (
        <button
          type="button"
          onClick={() => {
            setOpen(true)
            setDone(null)
          }}
          className="min-h-11 rounded-md bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:opacity-90 sm:min-h-0"
          data-testid="add-idea"
        >
          Add your idea
        </button>
      ) : (
        <form onSubmit={submit} className="rounded-lg border border-border p-3" data-testid="add-idea-form">
          <label htmlFor="idea-quote" className="block text-[13px] font-medium text-foreground">
            What would you want {projectName} to do for you?
          </label>
          <p className="mt-0.5 text-[12px] text-muted-foreground">
            A problem you have, or something you wish it did. Your words go on the list as you write them, with your
            name.
          </p>
          <textarea
            id="idea-quote"
            value={quote}
            onChange={(e) => setQuote(e.target.value)}
            rows={3}
            maxLength={2000}
            className="mt-2 w-full rounded-md border border-input bg-input px-2 py-1.5 text-[13px] text-foreground"
          />
          {error && <p className="mt-1 text-[12px] text-destructive">{error}</p>}
          <div className="mt-2 flex gap-2">
            <button
              type="submit"
              disabled={busy || !quote.trim()}
              className="min-h-11 rounded-md bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground disabled:opacity-50 sm:min-h-0"
            >
              {busy ? 'Adding…' : 'Add idea'}
            </button>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="min-h-11 rounded-md border border-border px-3 py-1.5 text-[13px] text-foreground sm:min-h-0"
            >
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  )
}
