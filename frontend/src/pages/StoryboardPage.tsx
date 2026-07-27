import { useEffect, useState } from 'react'
import { useParams, useSearchParams, Link } from 'react-router-dom'
import { getStoryboard, leaveFeedback, type Storyboard } from '@/api/storyboards'
import { withBase } from '@/lib/basePath'
import { NoteComposer } from '@/components/storyboard/NoteComposer'

/**
 * The shared arc — several DDD narratives, in acts, as one link.
 *
 * Runs OUTSIDE the app shell (PublicLayout) so a `?t=<share_token>` viewer with
 * no Dimagi login is served; the API self-enforces and 404s on a wrong token, so
 * there is nothing to branch on here.
 *
 * Acts are NUMBERED because act order carries meaning — you cannot understand
 * the last act's drill without the first act's purchase order. Scene numbers on
 * the reviewer surface are just addresses, so they stay quiet there.
 */

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; data: Storyboard }
  | { kind: 'not_found' }
  | { kind: 'error'; message: string }

const ROMAN = ['I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX', 'X']

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
    <div className="min-h-screen bg-background text-foreground">
      <div className="mx-auto flex min-h-screen max-w-3xl items-center justify-center px-6 text-center">
        <div>{children}</div>
      </div>
    </div>
  )
}

function Board({ board, token }: { board: Storyboard; token: string | null }) {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <div className="mx-auto max-w-3xl px-6 py-12 md:py-16">
        <div className="mb-10 flex items-center justify-between">
          <span className="text-[11px] font-semibold uppercase tracking-[0.2em] text-muted-foreground">
            Canopy · Demo
          </span>
        </div>

        <header className="mb-4">
          <p className="mb-2 text-[11px] font-semibold uppercase tracking-[0.2em] text-primary">
            {board.acts.length === 1 ? 'One act' : `${board.acts.length} acts`}
          </p>
          <h1 className="text-4xl font-semibold leading-tight tracking-tight text-foreground md:text-5xl">
            {board.title}
          </h1>
          {board.lede && (
            <p className="mt-4 text-lg leading-relaxed text-foreground-secondary">{board.lede}</p>
          )}
        </header>

        {board.acts.map((act, i) => (
          <section key={`${act.title}-${i}`} className="mt-11 flex flex-col gap-4">
            <div className="flex items-baseline gap-3.5 border-b border-border pb-3">
              <span className="shrink-0 pt-0.5 text-[11px] font-bold tracking-[0.16em] text-primary">
                ACT {ROMAN[i] ?? i + 1}
              </span>
              <div>
                <h2 className="text-xl font-semibold leading-tight tracking-tight text-foreground">
                  {act.title}
                </h2>
                {act.prose && (
                  <p className="mt-1.5 text-sm leading-relaxed text-foreground-secondary">
                    {act.prose}
                  </p>
                )}
              </div>
            </div>

            {act.entries.map((entry) => (
              <EntryCard key={entry.narrative_slug} entry={entry} board={board} token={token} />
            ))}

            <NoteComposer
              capability={board.capability}
              anchorLabel={`On “${act.title}”`}
              cta="Leave a note on this act"
              defaults={{}}
              onSubmit={(payload) => leaveFeedback(board.slug, payload, token).then(() => undefined)}
            />
          </section>
        ))}

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
}: {
  entry: Storyboard['acts'][number]['entries'][number]
  board: Storyboard
  token: string | null
}) {
  return (
    <article className="grid grid-cols-1 gap-4 rounded-lg border border-border bg-card p-3.5 sm:grid-cols-[168px_1fr]">
      <div className="relative aspect-[16/10] overflow-hidden rounded-md border border-border bg-muted">
        {entry.published && entry.video_url ? (
          <video
            controls
            preload="metadata"
            className="h-full w-full object-cover"
            src={withBase(entry.video_url)}
          />
        ) : (
          <div className="grid h-full w-full place-items-center px-2 text-center text-[11px] text-foreground-subtle">
            Not filmed yet
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

        {entry.published && (
          <NoteComposer
            capability={board.capability}
            anchorLabel={`On “${entry.title}” · v${entry.version}`}
            cta="Leave a note on this demo"
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
