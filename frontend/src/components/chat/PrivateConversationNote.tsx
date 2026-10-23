import { useState } from 'react'
import { Lock } from 'lucide-react'

/** The one-line reason a sensitive conversation is private, and the details behind it. */
export const PRIVATE_LINE = 'Private — only the people in this conversation can read it'
export const PRIVATE_DETAILS =
  'Its messages are sealed to the people in it: canopy stores them encrypted and cannot read them back. ' +
  'Canopy does not share, export or mirror it, and no review or other AI reads it. ' +
  'Memory is off in private conversations: agents neither learn from it nor read your memory in it.'

/**
 * The always-visible mark of a sensitive conversation, small and quiet: a lock, one
 * line, and the details a click away (a tooltip too, for a pointer). The chat page
 * and the embedded panel both show it under their header.
 */
export function PrivateConversationNote() {
  const [open, setOpen] = useState(false)
  return (
    <div data-testid="sensitive-banner" className="border-b border-border px-3 py-1 text-[11px] text-muted-foreground">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        title={PRIVATE_DETAILS}
        className="inline-flex max-w-full items-center gap-1 text-left hover:text-foreground"
      >
        <Lock className="h-3 w-3 shrink-0" aria-hidden />
        <span className="truncate">{PRIVATE_LINE}</span>
      </button>
      {open && <p data-testid="sensitive-details" className="m-0 mt-0.5">{PRIVATE_DETAILS}</p>}
    </div>
  )
}
