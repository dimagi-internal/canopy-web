import { useEffect, useMemo, useState } from 'react'
import { useParams, useSearchParams, Link } from 'react-router-dom'
import { apiUrl } from '@/api/base'
import { leaveFeedback, type Capability } from '@/api/storyboards'
import { NoteComposer } from '@/components/storyboard/NoteComposer'
import { pairNarrationScenes } from '@/components/ddd/narrativeScenePairing'
import type { DddNarration } from '@/api/ddd'

/**
 * One narrative, scene by scene, for someone who does not care how it was made.
 *
 * Shows the story and nothing else — no gates, features, provenance,
 * actionability scores or findings. The operator console at
 * /w/:ws/ddd/:narrative keeps all of that and is linked from the foot; this is
 * the front door, not a second-class copy of it (canopy-web#290: "the current
 * DDD review screens are very complicated and something only I understand").
 *
 * The before/after appears ONLY on scenes that changed. Showing it on every
 * scene would make a two-line edit look like a rewrite.
 */

interface NarrativeRead {
  narrative_slug: string
  title: string
  story: string
  version: number | null
  previous_version: number | null
  narration: DddNarration[]
  previous_narration: DddNarration[]
  storyboard_slug: string
  storyboard_title: string
  capability: Capability
}

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; data: NarrativeRead }
  | { kind: 'not_found' }
  | { kind: 'error'; message: string }

export default function NarrativeReviewPage() {
  const { slug } = useParams()
  const [params] = useSearchParams()
  const token = params.get('t')
  const board = params.get('b')
  const [state, setState] = useState<LoadState>({ kind: 'loading' })

  useEffect(() => {
    let live = true
    if (!slug || !board) {
      setState({ kind: 'not_found' })
      return
    }
    setState({ kind: 'loading' })
    const q = token ? `?t=${encodeURIComponent(token)}` : ''
    fetch(
      apiUrl(
        `/api/storyboards/${encodeURIComponent(board)}/narratives/${encodeURIComponent(slug)}${q}`,
      ),
      { credentials: 'same-origin' },
    )
      .then(async (r) => {
        if (!live) return
        if (r.status === 404) return setState({ kind: 'not_found' })
        if (!r.ok) return setState({ kind: 'error', message: `Request failed (${r.status})` })
        setState({ kind: 'ready', data: await r.json() })
      })
      .catch((e: Error) => live && setState({ kind: 'error', message: e.message }))
    return () => {
      live = false
    }
  }, [slug, board, token])

  if (state.kind === 'loading') return <Centered>Loading…</Centered>
  if (state.kind === 'not_found')
    return (
      <Centered>
        <p className="text-foreground">This story isn’t available.</p>
        <p className="mt-1 text-sm text-muted-foreground">
          The link may be private or may have been replaced.
        </p>
      </Centered>
    )
  if (state.kind === 'error')
    return (
      <Centered>
        <p className="text-foreground">Couldn’t load this story.</p>
        <p className="mt-1 text-sm text-muted-foreground">{state.message}</p>
      </Centered>
    )

  return <Review data={state.data} token={token} />
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

function Review({ data, token }: { data: NarrativeRead; token: string | null }) {
  useEffect(() => {
    document.title = `${data.title} · ${data.storyboard_title} · Canopy`
  }, [data.title, data.storyboard_title])

  const pairs = useMemo(
    () => pairNarrationScenes(data.previous_narration, data.narration),
    [data.previous_narration, data.narration],
  )
  // Count the kinds separately: with the positional fallback for legacy
  // histories, a rewritten narrative can add a scene as well as reword others,
  // and calling a brand-new scene "changed" misreports what the reader is
  // looking at.
  const edited = pairs.filter((p) => p.status === 'changed').length
  const added = pairs.filter((p) => p.status === 'added').length
  const cut = pairs.filter((p) => p.status === 'removed').length
  const moved = edited + added + cut
  const q = token ? `?t=${encodeURIComponent(token)}` : ''

  // A cut scene is history, not part of the story being read. Numbering it
  // alongside the others made scene 4 read as scene 5 and made the footer
  // promise more scenes than the narrative has. Number only what is still in
  // the narrative; a cut scene gets a dash.
  const sceneNumbers = new Map<number, number>()
  pairs.forEach((p, i) => {
    if (p.status !== 'removed') sceneNumbers.set(i, sceneNumbers.size + 1)
  })
  const sceneCount = sceneNumbers.size

  return (
    <div className="min-h-screen bg-background text-foreground">
      <div className="mx-auto max-w-3xl px-6 py-12 md:py-16">
        <div className="mb-8 flex flex-wrap items-center justify-between gap-3">
          <Link
            to={`/storyboard/${data.storyboard_slug}${q}`}
            className="text-[12.5px] font-medium text-primary hover:underline"
          >
            ← {data.storyboard_title}
          </Link>
          {data.version != null && (
            <span className="rounded-full border border-info/30 bg-info/10 px-2 py-0.5 text-[11px] tabular-nums text-info">
              v{data.version}
            </span>
          )}
        </div>

        <header className="mb-6">
          <p className="mb-2 text-[11px] font-semibold uppercase tracking-[0.2em] text-primary">
            The story
          </p>
          <h1 className="text-3xl font-semibold leading-tight tracking-tight text-foreground">
            {data.title}
          </h1>
          {data.story && (
            <p className="mt-3 text-[15px] leading-relaxed text-foreground-secondary">{data.story}</p>
          )}
        </header>

        {data.previous_version != null && (
          <p className="mb-8 rounded-lg border border-warning/25 bg-warning/[0.08] px-3 py-2.5 text-[12.5px] text-muted-foreground">
            {moved === 0 ? (
              <>Nothing has changed since v{data.previous_version}.</>
            ) : (
              <>
                Since v{data.previous_version}:{' '}
                {[
                  edited && `${edited} scene${edited === 1 ? '' : 's'} reworded`,
                  added && `${added} new`,
                  cut && `${cut} cut`,
                ]
                  .filter(Boolean)
                  .join(', ')}{' '}
                — marked below.
              </>
            )}
          </p>
        )}

        <div>
          {pairs.map((pair, i) => {
            const n = sceneNumbers.get(i)
            const isCut = pair.status === 'removed'
            return (
            <section
              key={pair.id ?? i}
              className="flex flex-col gap-3 border-t border-border py-5 first:border-t-0"
            >
              <div className="flex flex-wrap items-baseline gap-2.5">
                <span className="font-mono text-[11px] tabular-nums text-foreground-subtle">
                  {n != null ? String(n).padStart(2, '0') : '—'}
                </span>
                {pair.title && (
                  <h2 className="text-[15px] font-semibold text-foreground">{pair.title}</h2>
                )}
                {pair.status !== 'unchanged' && (
                  // A cut scene is not an addition; badging it in the same
                  // green said "look at this new thing" about something that is
                  // no longer there.
                  <span
                    className={
                      isCut
                        ? 'rounded-full border border-border bg-muted px-2 py-0.5 text-[11px] text-muted-foreground'
                        : 'rounded-full border border-success/30 bg-success/10 px-2 py-0.5 text-[11px] text-success'
                    }
                  >
                    {pair.status === 'added'
                      ? 'New'
                      : isCut
                        ? `Cut since v${data.previous_version}`
                        : 'Changed'}
                  </span>
                )}
              </div>

              {pair.status === 'changed' && pair.before ? (
                <div className="grid gap-3.5 sm:grid-cols-2">
                  <div className="flex flex-col gap-1.5">
                    <span className="text-[10px] uppercase tracking-[0.16em] text-foreground-subtle">
                      Was — v{data.previous_version}
                    </span>
                    <p className="text-[13.5px] leading-relaxed text-foreground-subtle line-through decoration-foreground-subtle/50">
                      {pair.before}
                    </p>
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <span className="text-[10px] uppercase tracking-[0.16em] text-foreground-subtle">
                      Now — v{data.version}
                    </span>
                    <p className="text-[13.5px] leading-relaxed text-foreground">{pair.after}</p>
                  </div>
                </div>
              ) : (
                <p
                  className={
                    isCut
                      ? 'text-[13.5px] leading-relaxed text-foreground-subtle line-through decoration-foreground-subtle/50'
                      : 'text-[13.5px] leading-relaxed text-foreground-secondary'
                  }
                >
                  {pair.after ?? pair.before}
                </p>
              )}

              <NoteComposer
                capability={data.capability}
                anchorLabel={
                  isCut
                    ? `Cut scene · v${data.previous_version}`
                    : `Scene ${n}${data.version != null ? ` · v${data.version}` : ''}`
                }
                cta={isCut ? 'Leave a note on the cut scene' : `Leave a note on scene ${n}`}
                defaults={{
                  narrative_slug: data.narrative_slug,
                  // A cut scene exists only in the previous version, so filing
                  // the note against the current one points it at nothing.
                  target_version: isCut ? data.previous_version : data.version,
                  anchor_id: pair.id ?? '',
                }}
                onSubmit={(payload) =>
                  leaveFeedback(data.storyboard_slug, payload, token).then(() => undefined)
                }
              />
            </section>
            )
          })}
        </div>

        <footer className="mt-14 flex flex-wrap items-center justify-between gap-2 border-t border-border pt-6 text-[11px] text-muted-foreground">
          <span>
            <span className="font-mono">{data.narrative_slug}</span>
            {data.version != null && ` · v${data.version}`} · {sceneCount} scenes
          </span>
        </footer>
      </div>
    </div>
  )
}
