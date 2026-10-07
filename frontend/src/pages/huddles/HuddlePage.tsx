import { useEffect, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { WorkbenchSkeleton } from 'canopy-ui'
import { getHuddle, type Huddle } from '@/api/huddles'
import { relativeTime } from '@/components/activity/turnLog'
import { HuddleConversation } from './HuddleConversation'
import { readView } from './conversationModel'
import { MemberAvatar } from './HuddleGrid'
import { HuddleOutcome } from './HuddleOutcome'
import { columns, countdown, roundName, roundsToShow } from './huddleModel'
import { andList, huddleExplainer, who } from './plainWords'

/**
 * One huddle, for a person seeing it cold: a one-line explainer of what a
 * huddle is, what was decided and what waits on the reader, then how it went —
 * the Story by default (three plain steps and the result), the Full
 * conversation (by agent / by idea, verbatim) or the Diagram. Every word a
 * reader sees comes from plainWords; the engine's own terms stay in the data.
 * Polls while the huddle is in flight; everything on it is derived
 * server-side from turns and tasks (apps/huddles).
 */

const POLL_MS = 15_000

function Pill({ className, children }: { className: string; children: React.ReactNode }) {
  return (
    <span className={`inline-flex h-5 items-center gap-1.5 rounded-full border px-2 text-[11px] font-medium ${className}`}>
      {children}
    </span>
  )
}

function Stepper({ huddle }: { huddle: Huddle }) {
  const rounds = roundsToShow(huddle)
  return (
    <ol aria-label="Steps" className="flex flex-wrap items-center gap-2">
      {rounds.map((r, i) => {
        const cells = huddle.cells.filter((c) => c.round === r)
        const replied = cells.filter((c) => c.block).length
        const done = huddle.finished || (cells.length > 0 && replied >= cells.length && r < huddle.rounds_dispatched)
        const current = !huddle.finished && r === huddle.rounds_dispatched
        return (
          <li key={r} className="flex items-center gap-2">
            {i > 0 && <span aria-hidden className={'h-px w-4 sm:w-6 ' + (r <= huddle.rounds_dispatched ? 'bg-primary/50' : 'bg-border')} />}
            <span
              className={
                'inline-flex items-center gap-2 rounded-full border px-2.5 py-1 text-[12px] ' +
                (current
                  ? 'border-primary/50 bg-primary/10 text-foreground'
                  : done
                    ? 'border-border bg-card text-foreground-secondary'
                    : 'border-dashed border-border text-muted-foreground')
              }
            >
              <span
                className={
                  'inline-flex size-5 items-center justify-center rounded-full text-[11px] font-semibold ' +
                  (current ? 'bg-primary text-primary-foreground' : done ? 'bg-success/20 text-success' : 'bg-muted text-muted-foreground')
                }
              >
                {done && !current ? '✓' : r}
              </span>
              {roundName(huddle.type, r)}
              {current && cells.length > 0 && (
                <span className="text-[11px] text-muted-foreground">{replied} of {cells.length} answered</span>
              )}
            </span>
          </li>
        )
      })}
    </ol>
  )
}

export function HuddlePage() {
  const { workspace = '', id = '' } = useParams()
  const [huddle, setHuddle] = useState<Huddle | null>(null)
  const [error, setError] = useState('')
  const [now, setNow] = useState(() => new Date())
  const [params] = useSearchParams()
  // The Story ends with the full result, so above it the summary stays short.
  const story = readView(params.get('view')) === 'story'

  useEffect(() => {
    let alive = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const load = () => {
      getHuddle(id)
        .then((h) => {
          if (!alive) return
          setHuddle(h)
          setError('')
          setNow(new Date())
          if (!h.finished) timer = setTimeout(load, POLL_MS)
        })
        .catch((e: unknown) => {
          if (!alive) return
          setError(e instanceof Error ? e.message : String(e))
          timer = setTimeout(load, POLL_MS * 2)
        })
    }
    load()
    return () => {
      alive = false
      if (timer) clearTimeout(timer)
    }
  }, [id])

  if (!huddle) {
    return (
      <div className="mx-auto max-w-7xl p-6">
        <Link to={`/w/${workspace}/huddles`} className="text-[12px] text-muted-foreground hover:text-primary">← Huddles</Link>
        {error ? <p className="mt-4 text-sm text-destructive">Couldn’t load this huddle: {error}</p> : <div className="mt-4"><WorkbenchSkeleton /></div>}
      </div>
    )
  }

  const due = countdown(huddle.deadline_at, now)
  const when = huddle.created_at
    ? new Date(huddle.created_at).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
    : ''
  return (
    // pb-28: room under the last cards for the site-wide floating "Canopy AI"
    // button, which otherwise covers the bottom-right member card.
    <div className="pb-28">
      <Link to={`/w/${workspace}/huddles`} className="text-[12px] text-muted-foreground hover:text-primary">← Huddles</Link>

      <header className="mt-3 mb-6 rounded-2xl border border-border bg-card p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0 max-w-[640px]">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold text-foreground">
                {who(huddle.leader)}&apos;s huddle{when ? <span className="font-normal text-muted-foreground"> · {when}</span> : null}
              </h1>
              {huddle.finished ? (
                <Pill className="border-success/30 bg-success/10 text-success">finished</Pill>
              ) : (
                <Pill className="border-info/30 bg-info/10 text-info">
                  <span className="size-1.5 animate-pulse rounded-full bg-info" /> still going
                </Pill>
              )}
            </div>
            <p className="mt-1.5 text-[14px] leading-relaxed text-foreground-secondary">{huddleExplainer(huddle.leader)}</p>
            <p className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-foreground-secondary">
              <span className="inline-flex items-center gap-1.5">
                <MemberAvatar slug={huddle.leader} hue="var(--foreground-secondary)" size="sm" />
                led by <span className="font-medium text-foreground">{who(huddle.leader)}</span>
              </span>
              <span className="text-muted-foreground">·</span>
              <span>with {andList(columns(huddle))}</span>
              <span className="text-muted-foreground">·</span>
              <span>started {relativeTime(huddle.created_at, now)}</span>
              {!huddle.finished && huddle.deadline_at && (
                <>
                  <span className="text-muted-foreground">·</span>
                  <span className={due ? '' : 'text-warning'}>{due ? `answers due in ${due}` : 'answers are late'}</span>
                </>
              )}
            </p>
          </div>
          <Stepper huddle={huddle} />
        </div>
      </header>

      <HuddleOutcome huddle={huddle} compact={story} />

      <section aria-label="How it went" className="mt-10 border-t border-border pt-6">
        <HuddleConversation huddle={huddle} />
      </section>
      {error && <p className="mt-4 text-[12px] text-warning">Live refresh paused: {error}</p>}
    </div>
  )
}

export default HuddlePage
