import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { WorkbenchSkeleton, WorkbenchSubHeader } from 'canopy-ui'
import { getHuddle, type Huddle, type HuddleOutput } from '@/api/huddles'
import { relativeTime } from '@/components/activity/turnLog'
import { ArcLegend, HuddleGrid, MemberAvatar } from './HuddleGrid'
import { columns, countdown, memberHue, roundName, roundsToShow } from './huddleModel'

/**
 * One huddle: the conversation as a grid (members × rounds) with the co-sign
 * arcs, then what it produced — board tasks, with their LIVE status — then the
 * leader's close. Polls while the huddle is in flight; everything on it is
 * derived server-side from turns and tasks (apps/huddles).
 */

const POLL_MS = 15_000

const TASK_STATUS: Record<string, string> = {
  suggested: 'bg-special/10 text-special border-special/30',
  in_progress: 'bg-info/10 text-info border-info/30',
  done: 'bg-success/10 text-success border-success/30',
  declined: 'bg-destructive/10 text-destructive border-destructive/30',
}

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

function Outputs({ huddle }: { huddle: Huddle }) {
  const cols = columns(huddle)
  const hueOf = (slug: string) => memberHue(Math.max(0, cols.indexOf(slug)))
  if (huddle.outputs.length === 0) {
    return (
      <p className="rounded-xl border border-dashed border-border p-4 text-[13px] text-muted-foreground">
        Nothing filed yet. When {huddle.leader} files the huddle, each agreed piece of work lands on its lead&apos;s
        board as a suggested task — and its status shows here as it moves.
      </p>
    )
  }
  return (
    <ul className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-card">
      {huddle.outputs.map((o: HuddleOutput) => (
        <li key={`${o.agent}-${o.task_id}`} className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
          <MemberAvatar slug={o.agent} hue={hueOf(o.agent)} size="sm" />
          <div className="min-w-0 flex-1">
            <div className="truncate text-[14px] font-medium text-foreground">{o.title}</div>
            <div className="mt-0.5 text-[12px] text-muted-foreground">
              {o.agent}&apos;s board
              {o.project && <> · project {o.project}</>}
              {o.assigned && <> · ball with {o.assigned}</>}
            </div>
          </div>
          <Pill className={TASK_STATUS[o.status] ?? 'bg-muted text-muted-foreground border-border'}>
            {o.status.replace('_', ' ')}
          </Pill>
          <Link to={o.url} className="text-[12px] text-primary underline-offset-2 hover:underline">
            {o.ext_id} on the board →
          </Link>
        </li>
      ))}
    </ul>
  )
}

export function HuddlePage() {
  const { workspace = '', id = '' } = useParams()
  const [huddle, setHuddle] = useState<Huddle | null>(null)
  const [error, setError] = useState('')
  const [now, setNow] = useState(() => new Date())
  const [showArcs, setShowArcs] = useState(true)

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
    <div className="pb-6">
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

      {/* The conversation breaks out of the page column: it is as wide as the
          team, and a fleet of five does not fit the reading width. */}
      <section className="relative left-1/2 w-[min(calc(100vw-3rem),1800px)] -translate-x-1/2">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-base font-semibold text-foreground">The conversation</h2>
        <div className="flex flex-wrap items-center gap-4">
          {showArcs && <ArcLegend />}
          {showArcs && <span className="hidden text-[11px] text-foreground-subtle lg:inline">hover a proposal or answer to trace it</span>}
          <label className="inline-flex cursor-pointer items-center gap-1.5 text-[12px] text-muted-foreground">
            <input type="checkbox" checked={showArcs} onChange={(e) => setShowArcs(e.target.checked)} className="accent-[var(--primary)]" />
            co-sign arcs
          </label>
        </div>
      </div>
      <HuddleGrid huddle={huddle} showArcs={showArcs} />
      </section>

      <section className="mt-10">
        <WorkbenchSubHeader title="What it produced" count={huddle.outputs.length} />
        <Outputs huddle={huddle} />
      </section>

      {huddle.summary && (
        <section className="mt-10">
          <WorkbenchSubHeader title={`${huddle.leader}'s close`} />
          <div className="whitespace-pre-wrap rounded-xl border border-border bg-card p-4 text-[13px] leading-relaxed text-foreground-secondary">
            {huddle.summary}
          </div>
        </section>
      )}
      {error && <p className="mt-4 text-[12px] text-warning">Live refresh paused: {error}</p>}
    </div>
  )
}

export default HuddlePage
