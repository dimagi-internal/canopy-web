import type { RoundNote, RoundPayload } from '@/api/storyboards'

/** The server marks a signed-in author's notes; a guest is known only by the
 *  name they typed, which NoteComposer remembers on this device. */
export function mineOf(notes: RoundNote[], viewerName: string): RoundNote[] {
  const name = viewerName.trim().toLowerCase()
  return notes.filter((n) => n.mine || (name !== '' && n.author_name.trim().toLowerCase() === name))
}

function outcome(n: RoundNote): string {
  if (n.state === 'answered') return n.resolved_in_version ? `Changed in version ${n.resolved_in_version}` : 'Answered'
  if (n.state === 'declined') return 'Not changed'
  return 'Not answered yet'
}

export function MyNotes({ round, viewerName }: { round: RoundPayload; viewerName: string }) {
  const mine = mineOf(round.notes, viewerName)
  if (mine.length === 0) return null
  return (
    <section aria-label="Your notes" className="mt-10 flex flex-col gap-3">
      <h2 className="text-lg font-semibold text-foreground">
        What happened to your notes from round {round.number}
      </h2>
      <ul className="flex flex-col gap-2">
        {mine.map((n) => (
          <li key={n.id} className="rounded-md border border-border bg-card p-3 text-sm">
            <p className="whitespace-pre-wrap break-words text-foreground">{n.body || n.suggested_text}</p>
            <p className="mt-1 font-medium text-foreground">{outcome(n)}</p>
            {n.disposition_note && <p className="text-muted-foreground">{n.disposition_note}</p>}
          </li>
        ))}
      </ul>
    </section>
  )
}
