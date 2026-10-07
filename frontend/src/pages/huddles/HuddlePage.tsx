import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { WorkbenchSkeleton } from 'canopy-ui'
import { getHuddle, type Huddle } from '@/api/huddles'
import { relativeTime } from '@/components/activity/turnLog'
import { HuddleConversation } from './HuddleConversation'
import { MemberAvatar } from './HuddleGrid'
import { HuddleOutcome, Linkified } from './HuddleOutcome'
import { columns, countdown, roundName, roundsToShow } from './huddleModel'

/**
 * One huddle, outcome first: what was decided — each filed proposal with the
 * board tasks it became (LIVE status), each held one with why — then the
 * conversation that got there (members × rounds, with the co-sign arcs) and the
 * leader's emailed close. The conversation opens as a readable transcript (by
 * agent or by proposal), with the rounds × members map one switch away. Polls while the
 * huddle is in flight; everything on it is derived server-side from turns and
 * tasks (apps/huddles).
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
  const members = columns(huddle).length || 1
  return (
    <ol className="flex flex-wrap items-center gap-2">
      {rounds.map((r, i) => {
        const cells = huddle.cells.filter((c) => c.round === r)
        const replied = cells.filter((c) => c.block).length
        const done = huddle.finished || (cells.length > 0 && replied >= cells.length && r < huddle.rounds_dispatched)
        const current = !huddle.finished && r === huddle.rounds_dispatched
        return (
          <li key={r} className="flex items-center gap-2">
            {i > 0 && <span aria-hidden className={'h-px w-6 ' + (r <= huddle.rounds_dispatched ? 'bg-primary/50' : 'bg-border')} />}
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
              {cells.length > 0 && (
                <span className="text-[11px] text-muted-foreground">
                  {replied}/{Math.max(cells.length, members)}
                </span>
              )}
            </span>
          </li>
        )
      })}
    </ol>
  )
}

/** "3 rounds, 12 replies" — sized so the toggle says what it hides. */
function conversationSize(h: Huddle): string {
  const rounds = new Set(h.cells.map((c) => c.round)).size
  const replies = h.cells.filter((c) => c.block).length
  return `${rounds} round${rounds === 1 ? '' : 's'}, ${replies} repl${replies === 1 ? 'y' : 'ies'}`
}

export function HuddlePage() {
  const { workspace = '', id = '' } = useParams()
  const [huddle, setHuddle] = useState<Huddle | null>(null)
  const [error, setError] = useState('')
  const [now, setNow] = useState(() => new Date())
  // Open by default: the cards start compact, so the whole flow fits under the
  // outcome instead of the ~7,500px wall it used to be.
  const [showConversation, setShowConversation] = useState(true)

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
  return (
    // pb-28: room under the last cards for the site-wide floating "Canopy AI"
    // button, which otherwise covers the bottom-right member card.
    <div className="pb-28">
      <Link to={`/w/${workspace}/huddles`} className="text-[12px] text-muted-foreground hover:text-primary">← Huddles</Link>

      <header className="mt-3 mb-6 rounded-2xl border border-border bg-card p-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold text-foreground">
                <span className="capitalize">{huddle.type || 'team'}</span> huddle
              </h1>
              {huddle.finished ? (
                <Pill className="border-success/30 bg-success/10 text-success">filed</Pill>
              ) : (
                <Pill className="border-info/30 bg-info/10 text-info">
                  <span className="size-1.5 animate-pulse rounded-full bg-info" /> in flight
                </Pill>
              )}
            </div>
            <p className="mt-1 font-mono text-[12px] text-muted-foreground">{huddle.id}</p>
            <p className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-foreground-secondary">
              <span className="inline-flex items-center gap-1.5">
                <MemberAvatar slug={huddle.leader} hue="var(--foreground-secondary)" size="sm" />
                led by <span className="font-medium text-foreground">{huddle.leader}</span>
              </span>
              <span className="text-muted-foreground">·</span>
              <span>team {huddle.team || '—'}</span>
              <span className="text-muted-foreground">·</span>
              <span>started {relativeTime(huddle.created_at, now)}</span>
              {!huddle.finished && huddle.deadline_at && (
                <>
                  <span className="text-muted-foreground">·</span>
                  <span className={due ? '' : 'text-warning'}>{due ? `replies due in ${due}` : 'deadline passed'}</span>
                </>
              )}
            </p>
          </div>
          <Stepper huddle={huddle} />
        </div>
      </header>

      <HuddleOutcome huddle={huddle} />

      {huddle.summary && (
        <details data-close className="mt-6 text-[13px]">
          <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
            The email {huddle.leader} sent
          </summary>
          <div className="mt-2 whitespace-pre-wrap break-words rounded-xl border border-border bg-card p-4 leading-relaxed text-foreground-secondary">
            <Linkified text={huddle.summary} />
          </div>
        </details>
      )}

      <section className="mt-10 border-t border-border pt-6">
        <button
          type="button"
          onClick={() => setShowConversation(!showConversation)}
          aria-expanded={showConversation}
          aria-controls="huddle-conversation"
          className="inline-flex items-center gap-2 rounded-lg border border-border bg-card px-3 py-2 text-[13px] font-medium text-foreground hover:border-primary/50 hover:text-primary"
        >
          <span aria-hidden className={'inline-block transition-transform ' + (showConversation ? 'rotate-90' : '')}>▸</span>
          {showConversation ? 'Hide the conversation' : `Show the full conversation — ${conversationSize(huddle)}`}
        </button>
        {!showConversation && (
          <p className="mt-2 text-[12px] text-muted-foreground">
            How the team got here: what the leader asked each member, what they said back, and how they answered each other&apos;s proposals.
          </p>
        )}
      </section>

      {showConversation && (
        <section id="huddle-conversation" aria-label="The conversation" className="mt-4">
          <HuddleConversation huddle={huddle} />
        </section>
      )}
      {error && <p className="mt-4 text-[12px] text-warning">Live refresh paused: {error}</p>}
    </div>
  )
}

export default HuddlePage
