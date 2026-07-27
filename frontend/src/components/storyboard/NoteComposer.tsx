import { useState } from 'react'
import type { Capability, FeedbackKind, LeaveFeedbackIn } from '@/api/storyboards'

/**
 * One composer, used at act level and scene level.
 *
 * Comment and suggest are a single control rather than two buttons: they
 * produce the same kind of thing (a note attached to a place in the story) and
 * differ only in whether the reviewer is proposing replacement words. The
 * "Suggest wording" half only exists when the link grants it.
 */
export function NoteComposer({
  capability,
  anchorLabel,
  cta,
  defaults,
  onSubmit,
}: {
  capability: Capability
  anchorLabel: string
  /** What the button says. Two composers can sit next to each other (an act and
   *  the demo inside it), so "Leave a note" twice is ambiguous — name the
   *  target. */
  cta: string
  defaults: LeaveFeedbackIn
  onSubmit: (payload: LeaveFeedbackIn) => Promise<void>
}) {
  const canSuggest = capability === 'suggest'
  const [open, setOpen] = useState(false)
  const [kind, setKind] = useState<FeedbackKind>('comment')
  const [text, setText] = useState('')
  const [name, setName] = useState('')
  const [state, setState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [error, setError] = useState('')

  if (capability === 'read') return null

  if (state === 'saved') {
    return (
      <p className="mt-3 text-[13px] text-success" role="status">
        Thanks — your note is with the team.
      </p>
    )
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="mt-3 self-start rounded-md border border-border px-3 py-1 text-[12px] text-foreground-secondary transition-colors hover:border-input hover:text-primary"
      >
        {cta}
      </button>
    )
  }

  const submit = async () => {
    setState('saving')
    setError('')
    try {
      await onSubmit({
        ...defaults,
        kind,
        body: kind === 'comment' ? text : '',
        suggested_text: kind === 'suggestion' ? text : '',
        author_name: name,
      })
      setState('saved')
    } catch (e) {
      setState('error')
      setError((e as Error).message)
    }
  }

  return (
    <div className="mt-3 flex flex-col gap-2 rounded-lg border border-border bg-card p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        {canSuggest ? (
          <div className="inline-flex overflow-hidden rounded-md border border-border" role="group" aria-label="Note type">
            {(['comment', 'suggestion'] as const).map((k) => (
              <button
                key={k}
                type="button"
                aria-pressed={kind === k}
                onClick={() => setKind(k)}
                className={`px-3 py-1 text-[12px] transition-colors ${
                  kind === k ? 'bg-muted text-foreground' : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                {k === 'comment' ? 'Comment' : 'Suggest wording'}
              </button>
            ))}
          </div>
        ) : (
          <span className="text-[11px] uppercase tracking-[0.16em] text-muted-foreground">Comment</span>
        )}
        <span className="text-[11px] text-foreground-subtle">{anchorLabel}</span>
      </div>

      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={3}
        placeholder={
          kind === 'suggestion' ? 'Propose the wording you’d use instead…' : 'What’s wrong, missing, or worth saying differently?'
        }
        className="w-full rounded-md border border-input bg-background px-3 py-2 text-[13px] text-foreground placeholder:text-foreground-subtle focus-visible:outline-2 focus-visible:outline-primary"
      />

      <div className="flex flex-wrap items-center justify-between gap-2">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Your name (optional)"
          className="min-w-0 flex-1 rounded-md border border-input bg-background px-3 py-1.5 text-[12px] text-foreground placeholder:text-foreground-subtle focus-visible:outline-2 focus-visible:outline-primary"
        />
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="rounded-md px-2 py-1 text-[12px] text-muted-foreground hover:text-foreground"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={!text.trim() || state === 'saving'}
            className="rounded-md bg-primary px-3 py-1.5 text-[12px] font-semibold text-primary-foreground transition-[filter] hover:brightness-95 disabled:opacity-50"
          >
            {state === 'saving' ? 'Saving…' : 'Leave note'}
          </button>
        </div>
      </div>

      {state === 'error' && (
        <p className="text-[12px] text-destructive" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}
