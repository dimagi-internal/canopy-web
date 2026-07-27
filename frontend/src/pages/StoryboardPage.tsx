import { useEffect, useState } from 'react'
import { useParams, useSearchParams, Link } from 'react-router-dom'
import {
  getStoryboard,
  getStoryboardNotes,
  leaveFeedback,
  type Note,
  type Storyboard,
} from '@/api/storyboards'
import { withBase } from '@/lib/basePath'
import { NoteComposer } from '@/components/storyboard/NoteComposer'
import { NotesReturned, groupNotes } from '@/components/storyboard/NotesReturned'
import { PublicHeader } from '@/components/PublicHeader'

/**
 * The shared arc — several DDD narratives as one link.
 *
 * Runs OUTSIDE the app shell (PublicLayout) so a `?t=<share_token>` viewer with
 * no Dimagi login is served; the API self-enforces and 404s on a wrong token, so
 * there is nothing to branch on here.
 *
 * The reader sees NARRATIVES, not acts. The storage layer still groups entries
 * under an Act — that is where the curated title and the connective prose live,
 * and it is what orders the arc — but an act holding a single narrative is not a
 * second thing to a reader. Naming it one produced the confusion this removed:
 * two composers on the same narrative, one reading "leave a note on this act"
 * and one "leave a note on this demo", identical in every way that showed.
 */

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; data: Storyboard }
  | { kind: 'not_found' }
  | { kind: 'error'; message: string }

export default function StoryboardPage() {
  const { slug } = useParams()
  const [params] = useSearchParams()
  const token = params.get('t')
  const [state, setState] = useState<LoadState>({ kind: 'loading' })

  useEffect(() => {
    let live = true
    if (!slug) {
      setState({ kind: 'not_found' })
      return
    }
    setState({ kind: 'loading' })
    getStoryboard(slug, token)
      .then((data) => live && setState({ kind: 'ready', data }))
      .catch((e: Error) => {
        if (!live) return
        setState(
          /not found|404/i.test(e.message || '')
            ? { kind: 'not_found' }
            : { kind: 'error', message: e.message },
        )
      })
    return () => {
      live = false
    }
  }, [slug, token])

  if (state.kind === 'loading') return <Centered>Loading…</Centered>
  if (state.kind === 'not_found')
    return (
      <Centered>
        <p className="text-foreground">This demo isn’t available.</p>
        <p className="mt-1 text-sm text-muted-foreground">
          The link may be private or may have been replaced.
        </p>
      </Centered>
    )
  if (state.kind === 'error')
    return (
      <Centered>
        <p className="text-foreground">Couldn’t load this demo.</p>
        <p className="mt-1 text-sm text-muted-foreground">{state.message}</p>
      </Centered>
    )

  return <Board board={state.data} token={token} />
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <PublicHeader />
      <div className="mx-auto flex max-w-3xl flex-1 items-center justify-center px-6 text-center">
        <div>{children}</div>
      </div>
    </div>
  )
}

function Board({ board, token }: { board: Storyboard; token: string | null }) {
  // A shared link gets bookmarked and sits among a dozen other tabs. "Canopy"
  // tells the reader nothing about which one this is.
  useEffect(() => {
    document.title = `${board.title} · Canopy`
  }, [board.title])

  // The closing half of the loop: the people who sent this link open the same
  // page to read what came back, in place. Reviewers never see it — the fetch
  // is only made for members and 401s for anyone else regardless.
  const [notes, setNotes] = useState<Note[]>([])
  useEffect(() => {
    if (!board.is_member) return
    let live = true
    getStoryboardNotes(board.slug)
      .then((r) => live && setNotes(r.items))
      .catch(() => undefined) // Nothing to read is not an error worth shouting.
    return () => {
      live = false
    }
  }, [board.is_member, board.slug])

  const grouped = groupNotes(notes)
  const narrativeCount = board.acts.reduce((n, a) => n + a.entries.length, 0)

  return (
    <div className="min-h-screen bg-background text-foreground">
      <PublicHeader />
      <div className="mx-auto max-w-3xl px-6 py-12 md:py-16">
        <header className="mb-4">
          <p className="mb-2 text-[11px] font-semibold uppercase tracking-[0.2em] text-primary">
            {narrativeCount === 1 ? 'One narrative' : `${narrativeCount} narratives`}
          </p>
          <h1 className="text-4xl font-semibold leading-tight tracking-tight text-foreground md:text-5xl">
            {board.title}
          </h1>
          {board.lede && (
            <p className="mt-4 text-lg leading-relaxed text-foreground-secondary">{board.lede}</p>
          )}

          {/* A domain expert opening a link cold has no way to know their notes
              are wanted, or who reads them. The composers are further down the
              page and their buttons only say what they do, not that doing it is
              the point of sending this. Members already know. */}
          {!board.is_member && board.capability !== 'read' && (
            <p className="mt-4 text-[13.5px] leading-relaxed text-muted-foreground">
              Watch each demo and read the scenes.{' '}
              <span className="text-foreground-secondary">
                Leave a note anywhere something is wrong, missing, or would be said
                differently
                {board.capability === 'suggest'
                  ? ' — or edit the narrative itself, in its own words'
                  : ''}
                .
              </span>{' '}
              Your notes go to the team that sent you this, and to nobody else.
            </p>
          )}
        </header>

        {board.acts.map((act, i) => (
          <section key={`${act.title}-${i}`} className="mt-11 flex flex-col gap-4">
            <div className="border-b border-border pb-3">
              <h2 className="text-xl font-semibold leading-tight tracking-tight text-foreground">
                {act.title}
              </h2>
              {act.prose && (
                <p className="mt-1.5 text-sm leading-relaxed text-foreground-secondary">
                  {act.prose}
                </p>
              )}
            </div>

            {/* Notes left before this page stopped naming a separate act layer.
                They still belong to this section, so they still render in it —
                nothing anchors to `act:*` any more, but nothing is dropped. */}
            <NotesReturned notes={grouped.byAnchor.get(act.anchor_id) ?? []} />

            {act.entries.map((entry) => (
              <EntryCard
                key={entry.narrative_slug}
                entry={entry}
                board={board}
                token={token}
                notes={grouped.byNarrative.get(entry.narrative_slug) ?? []}
              />
            ))}
          </section>
        ))}

        {grouped.loose.length > 0 && (
          <section className="mt-11 flex flex-col gap-4">
            <div className="border-b border-border pb-3">
              <h2 className="text-xl font-semibold leading-tight tracking-tight text-foreground">
                On the arc as a whole
              </h2>
            </div>
            <NotesReturned notes={grouped.loose} />
          </section>
        )}

        <footer className="mt-16 flex items-center justify-between border-t border-border pt-6 text-[11px] text-muted-foreground">
          <span>
            Generated by canopy · storyboard <span className="font-mono">{board.slug}</span>
          </span>
        </footer>
      </div>
    </div>
  )
}

function EntryCard({
  entry,
  board,
  token,
  notes,
}: {
  entry: Storyboard['acts'][number]['entries'][number]
  board: Storyboard
  token: string | null
  notes: Note[]
}) {
  return (
    <article className="grid grid-cols-1 gap-4 rounded-lg border border-border bg-card p-3.5 sm:grid-cols-[168px_1fr]">
      <div className="relative aspect-[16/10] overflow-hidden rounded-md border border-border bg-muted">
        {entry.published && entry.video_url ? (
          <video
            controls
            preload="metadata"
            aria-label={`Demo video: ${entry.title}`}
            className="h-full w-full object-cover"
            src={withBase(entry.video_url)}
          />
        ) : (
          <div className="grid h-full w-full place-items-center px-2 text-center text-[11px] text-foreground-subtle">
            {/* Two different situations, and telling a reader "not filmed yet"
                about a demo that was filmed but never made public sends them
                to ask the wrong question. */}
            {entry.published ? 'Video not shared' : 'Not filmed yet'}
          </div>
        )}
      </div>

      <div className="flex min-w-0 flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-[15px] font-semibold text-foreground">{entry.title}</h3>
          {entry.published ? (
            <span className="rounded-full border border-info/30 bg-info/10 px-2 py-0.5 text-[11px] tabular-nums text-info">
              v{entry.version}
            </span>
          ) : (
            <span className="rounded-full border border-warning/30 bg-warning/10 px-2 py-0.5 text-[11px] text-warning">
              Being built
            </span>
          )}
        </div>

        {entry.lede && (
          <p className="text-[13.5px] leading-relaxed text-foreground-secondary">{entry.lede}</p>
        )}

        <div className="mt-0.5 flex flex-wrap items-center gap-3.5">
          {entry.published ? (
            <Link
              to={`/narrative/${entry.narrative_slug}?b=${encodeURIComponent(board.slug)}${token ? `&t=${encodeURIComponent(token)}` : ''}`}
              className="text-[12.5px] font-medium text-primary hover:underline"
            >
              Read the scenes →
            </Link>
          ) : (
            <span className="text-[12px] text-foreground-subtle">
              Nothing to watch until this one is built.
            </span>
          )}
        </div>

        <NotesReturned notes={notes} />

        {entry.published && (
          <NoteComposer
            capability={board.capability}
            anchorLabel={`On “${entry.title}” · v${entry.version}`}
            cta="Leave a note on this narrative"
            seedText={entry.lede}
            defaults={{ narrative_slug: entry.narrative_slug, target_version: entry.version }}
            onSubmit={(payload) =>
              leaveFeedback(board.slug, payload, token).then(() => undefined)
            }
          />
        )}
      </div>
    </article>
  )
}
