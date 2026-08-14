import { useEffect, useState } from 'react'
import { useAuth } from '@/auth/AuthProvider'
import type { Capability, FeedbackKind, LeaveFeedbackIn } from '@/api/storyboards'

/**
 * One composer, used on a narrative and on a scene.
 *
 * Comment and edit are a single control rather than two buttons: they produce
 * the same kind of thing (a note attached to a place in the story) and differ
 * only in whether the reviewer is rewriting the words. Commenting is a blank
 * box; editing OPENS THE TEXT ITSELF so you change it in place — describing an
 * edit in prose ("in the second sentence, say re-visit instead of check") is
 * work for the reader and a chance to be misread, when the reviewer already
 * knows the exact words they want. The edit half only exists when the link
 * grants it.
 */
const NAME_KEY = 'canopy.reviewer-name'

function readReviewerName(): string {
  try {
    return localStorage.getItem(NAME_KEY) ?? ''
  } catch {
    return '' // Private-mode browsers throw on access; a blank name is fine.
  }
}

function rememberReviewerName(value: string): void {
  try {
    localStorage.setItem(NAME_KEY, value)
  } catch {
    /* not worth failing a note over */
  }
}

export function NoteComposer({
  capability,
  anchorLabel,
  cta,
  seedText,
  defaults,
  onSubmit,
}: {
  capability: Capability
  anchorLabel: string
  /** What the button says. Two composers can sit next to each other (an act and
   *  the demo inside it), so "Leave a note" twice is ambiguous — name the
   *  target. */
  cta: string
  /** The words an edit starts from — the scene's current text, or the
   *  narrative's lede. Editing a copy of the real thing is the point; an empty
   *  box would just be the comment box again. */
  seedText?: string
  defaults: LeaveFeedbackIn
  onSubmit: (payload: LeaveFeedbackIn) => Promise<void>
}) {
  const canSuggest = capability === 'suggest'
  const [open, setOpen] = useState(false)
  const [kind, setKind] = useState<FeedbackKind>('comment')
  const [text, setText] = useState('')
  // Asked once, not once per note. A full review puts a composer under every
  // narrative and every scene — better than twenty "Anonymous" notes from the
  // one person you sent the link to.
  const [name, setName] = useState(() => readReviewerName())
  const [touchedName, setTouchedName] = useState(false)

  // Signed in? Then we already know who is editing and should not ask. Read it
  // from the auth state the app has anyway — the reviewer surfaces render
  // inside AuthProvider even though they are public, so a second /api/me/ per
  // composer would be a duplicate of a request already made. Never overwrite
  // what they typed: a member filing a note under somebody else's name is their
  // call, not ours.
  const auth = useAuth()
  const signedInName = auth.status === 'authenticated' ? auth.user.name || auth.user.email : ''
  useEffect(() => {
    if (signedInName && !touchedName) setName((current) => current || signedInName)
  }, [signedInName, touchedName])
  const [state, setState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [error, setError] = useState('')

  if (capability === 'read') return null

  if (state === 'saved') {
    // A reviewer going through a narrative leaves several notes on the same
    // scene, not one. Ending in a dead "thanks" meant the only way to add
    // another was to reload the page.
    return (
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <p className="text-[13px] text-success" role="status">
          Thanks — your note is with the team.
        </p>
        <button
          type="button"
          onClick={() => {
            setText('')
            setState('idle')
            setOpen(true)
          }}
          className="rounded-md border border-border px-2.5 py-1 text-[12px] text-foreground-secondary transition-colors hover:border-input hover:text-primary"
        >
          Add another
        </button>
      </div>
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
                onClick={() => {
                  setKind(k)
                  // Hand them the real words to mutate. Only when the box is
                  // untouched — switching tabs must never eat what someone
                  // has already written.
                  if (k === 'suggestion' && seedText && !text.trim()) setText(seedText)
                  if (k === 'comment' && text === seedText) setText('')
                }}
                className={`px-3 py-1 text-[12px] transition-colors ${
                  kind === k ? 'bg-muted text-foreground' : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                {k === 'comment' ? 'Comment' : 'Edit narrative'}
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
        rows={kind === 'suggestion' ? 6 : 3}
        placeholder={
          kind === 'suggestion'
            ? 'Change the words to what they should say…'
            : 'What’s wrong, missing, or worth saying differently?'
        }
        className="w-full rounded-md border border-input bg-background px-3 py-2 text-[13px] text-foreground placeholder:text-muted-foreground dark:placeholder:text-foreground-secondary focus-visible:outline-2 focus-visible:outline-primary"
      />

      <div className="flex flex-wrap items-center justify-between gap-2">
        <input
          value={name}
          onChange={(e) => {
            setTouchedName(true)
            setName(e.target.value)
            rememberReviewerName(e.target.value)
          }}
          placeholder="Your name (optional)"
          className="min-w-0 flex-1 rounded-md border border-input bg-background px-3 py-1.5 text-[12px] text-foreground placeholder:text-muted-foreground dark:placeholder:text-foreground-secondary focus-visible:outline-2 focus-visible:outline-primary"
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
