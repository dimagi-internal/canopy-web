// The pieces of a project's overview: marking an idea shipped, the standing list, and adding an idea. The overview and its data are `ProjectOverview`.
import { useState, type FormEvent, type JSX, } from 'react'
import { actOnTask, addIdea, AgentApiError, type IdeaOut, } from '@/api/agents'
import { StagePill } from '@/pages/agents/ideaParts'

export function MarkShipped({ agent, idea, onDone }: { agent: string; idea: IdeaOut; onDone: () => void }): JSX.Element {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  return (
    <span className="inline-flex items-center gap-1">
      <button
        type="button"
        disabled={busy}
        onClick={() => {
          // One click otherwise moves it to Built for everyone, sitting next to every idea.
          if (!window.confirm(`Mark “${idea.title || idea.quote}” shipped? It moves to Built.`)) return
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

export function StandingList({
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

export function AddIdea({
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
