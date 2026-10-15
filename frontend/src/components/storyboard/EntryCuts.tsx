import { useState } from 'react'
import type { EntryCut, LeaveFeedbackIn, Note, Storyboard, StoryboardEntry } from '@/api/storyboards'
import { AppLinks } from '@/components/ddd/AppLinks'
import { CutPlayer, fmtDuration } from '@/components/ddd/CutPlayer'
import { NoteComposer } from '@/components/storyboard/NoteComposer'
import { NotesReturned } from '@/components/storyboard/NotesReturned'
import { cutDomId } from '@/components/storyboard/DemoBrief'

/**
 * A recorded narrative's cuts, on the board itself.
 *
 * The board used to show one hero video per narrative, so the cuts a set of
 * demos is actually made of — and everything a reviewer needs to judge one
 * (its length, its words, what is on screen, why it matters to this audience,
 * where the data comes from) — lived in a separate doc. Each cut also takes a
 * note of its own: a reviewer's most useful comment is often about a cut as a
 * whole ("too fast on the tender row", "show the map first"), not a word in it.
 */
export function EntryCuts({
  entry,
  board,
  notes,
  onSubmit,
}: {
  entry: StoryboardEntry
  board: Storyboard
  /** This entry's notes; the ones anchored to a cut render under that cut. */
  notes: Note[]
  onSubmit: (payload: LeaveFeedbackIn) => Promise<void>
}) {
  const cuts = entry.cuts ?? []
  return (
    <ol className="flex flex-col gap-6">
      {cuts.map((cut, i) => (
        <li key={cut.cut_id} id={cutDomId(entry.narrative_slug, cut.cut_id)} className="scroll-mt-6">
          <CutBlock
            n={i + 1}
            cut={cut}
            entry={entry}
            board={board}
            notes={notes.filter((x) => x.anchor_id === cut.anchor_id)}
            onSubmit={onSubmit}
          />
        </li>
      ))}
    </ol>
  )
}

function CutBlock({
  n,
  cut,
  entry,
  board,
  notes,
  onSubmit,
}: {
  n: number
  cut: EntryCut
  entry: StoryboardEntry
  board: Storyboard
  notes: Note[]
  onSubmit: (payload: LeaveFeedbackIn) => Promise<void>
}) {
  // The upload may not have recorded a length; the video itself knows it.
  const [measured, setMeasured] = useState<number | null>(null)
  const duration = fmtDuration(cut.duration_sec ?? measured)
  const words = cut.scenes.map((s) => s.text).join(' ')
  // A reel is the finished product: the video and its name, nothing to read
  // or answer. Everything below the player is review furniture.
  const reel = board.layout === 'reel'

  return (
    <section aria-label={cut.title} className="flex flex-col gap-3">
      <h4 className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-[15px] font-semibold text-foreground">
        <span className="tabular-nums text-muted-foreground">{n}.</span>
        <span>{cut.title}</span>
        {cut.optional && (
          <span className="rounded border border-border px-1.5 py-0.5 text-[10px] font-normal text-muted-foreground">
            optional
          </span>
        )}
        <span className="text-[12px] font-normal tabular-nums text-muted-foreground">
          {[duration, cut.word_count ? `${cut.word_count} words` : null].filter(Boolean).join(' · ')}
        </span>
      </h4>

      <div className={reel ? '' : 'grid gap-4 md:grid-cols-[3fr_2fr]'}>
        <div className="overflow-hidden rounded-lg border border-input bg-black">
          {cut.video_url ? (
            <CutPlayer
              src={cut.video_url}
              poster={cut.poster_url}
              label={cut.title}
              onDuration={setMeasured}
            />
          ) : (
            <div className="flex aspect-video items-center justify-center bg-card px-4 text-center text-sm text-muted-foreground">
              {cut.recorded ? 'This cut is not shared on this link.' : 'Not recorded yet.'}
            </div>
          )}
        </div>
        {!reel && (
        <div className="flex flex-col gap-2 text-[13.5px] leading-relaxed text-foreground-secondary">
          {cut.scenes.length > 0 ? (
            cut.scenes.map((s) => <p key={s.id}>{s.text}</p>)
          ) : (
            <p className="text-muted-foreground">No narration recorded for this cut.</p>
          )}
        </div>
        )}
      </div>

      {!reel && (cut.screen_notes || cut.audience_note || cut.provenance || cut.app_links?.length) ? (
        <dl className="grid gap-1.5 text-[13px]">
          {cut.app_links?.length ? (
            <Detail label="Try it live">
              <AppLinks links={cut.app_links} />
            </Detail>
          ) : null}
          {cut.screen_notes && <Detail label="On screen">{cut.screen_notes}</Detail>}
          {cut.audience_note && <Detail label="Why it matters">{cut.audience_note}</Detail>}
          {cut.provenance && <Detail label="Data from">{cut.provenance}</Detail>}
        </dl>
      ) : null}

      {!reel ? (
        <>
          <NotesReturned notes={notes} />
          {entry.published && (
            <NoteComposer
              capability={board.capability}
              anchorLabel={`On cut ${n}, “${cut.title}”`}
              cta="Comment on this cut"
              seedText={words}
              defaults={{
                narrative_slug: entry.narrative_slug,
                target_version: entry.version,
                anchor_id: cut.anchor_id,
              }}
              onSubmit={onSubmit}
            />
          )}
        </>
      ) : null}
    </section>
  )
}

function Detail({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-0.5 sm:grid-cols-[7.5rem_1fr] sm:gap-3">
      <dt className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground sm:pt-0.5">{label}</dt>
      <dd className="text-foreground">{children}</dd>
    </div>
  )
}
