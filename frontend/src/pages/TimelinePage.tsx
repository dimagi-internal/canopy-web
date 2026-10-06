import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { clsx } from 'clsx'
import {
  WorkbenchShell,
  WorkbenchMain,
  WorkbenchRail,
  WorkbenchNavItem,
  EmptyState,
  ErrorState,
  LoadingSpinner,
} from 'canopy-ui'
import {
  listTimeline,
  type TimelineEvent,
  type TimelineSubsystem,
} from '@/api/timeline'

const PAGE_SIZE = 50

// A glyph per icon hint the backend stamps — a quiet visual anchor per row.
const ICON: Record<string, string> = {
  video: '▶',
  deck: '◫',
  doc: '⎘',
  narrative: '✎',
  note: '·',
  sync: '⟳',
  task: '◆',
  session: '⌘',
  skill: '✧',
}

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return 'now'
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  if (d < 7) return `${d}d ago`
  return new Date(iso).toLocaleDateString()
}

function dayLabel(iso: string): string {
  const d = new Date(iso)
  const today = new Date()
  const yesterday = new Date()
  yesterday.setDate(today.getDate() - 1)
  const sameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  if (sameDay(d, today)) return 'Today'
  if (sameDay(d, yesterday)) return 'Yesterday'
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
}

/** Group consecutive (already newest-first) events under their day label. */
function groupByDay(events: TimelineEvent[]): Array<{ day: string; events: TimelineEvent[] }> {
  const groups: Array<{ day: string; events: TimelineEvent[] }> = []
  for (const ev of events) {
    const day = dayLabel(ev.at)
    const last = groups[groups.length - 1]
    if (last && last.day === day) last.events.push(ev)
    else groups.push({ day, events: [ev] })
  }
  return groups
}

function EventRow({ ev, label }: { ev: TimelineEvent; label: string }) {
  const navigate = useNavigate()
  const glyph = (ev.icon && ICON[ev.icon]) || '•'

  const inner = (
    <>
      <div className="w-14 shrink-0 pt-0.5 text-right text-[11px] tabular-nums text-muted-foreground">
        {relativeTime(ev.at)}
      </div>
      <div
        aria-hidden
        className="w-4 shrink-0 pt-0.5 text-center text-xs text-muted-foreground"
      >
        {glyph}
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="rounded bg-accent px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground">
            {label}
          </span>
          <span className="truncate text-[13px] font-medium text-foreground">{ev.title}</span>
        </div>
        {ev.summary && (
          <div className="mt-0.5 truncate text-[12px] text-muted-foreground">{ev.summary}</div>
        )}
        <div className="mt-0.5 flex items-center gap-2 text-[11px] text-muted-foreground">
          {ev.project_slug && (
            <span className="rounded border border-border px-1 py-px text-muted-foreground">
              {ev.project_slug}
            </span>
          )}
          {ev.actor && <span className="truncate">{ev.actor}</span>}
        </div>
      </div>
    </>
  )

  const rowClass =
    'flex w-full gap-3 border-b border-border/60 px-4 py-2.5 text-left transition-colors hover:bg-accent'

  if (ev.external) {
    return (
      <a href={ev.href} target="_blank" rel="noopener noreferrer" className={rowClass}>
        {inner}
      </a>
    )
  }
  return (
    <button type="button" onClick={() => navigate(ev.href)} className={rowClass}>
      {inner}
    </button>
  )
}

function TimelineRail({
  subsystems,
  active,
}: {
  subsystems: TimelineSubsystem[]
  active: string | null
}) {
  return (
    <WorkbenchRail
      header={
        <div className="px-4 py-3">
          <div className="text-sm font-semibold text-foreground">Activity</div>
          <div className="text-[11px] text-muted-foreground">Across the workspace</div>
        </div>
      }
    >
      <nav className="flex flex-col gap-0.5 p-2">
        <WorkbenchNavItem asChild active={active === null}>
          <Link to="/timeline">All activity</Link>
        </WorkbenchNavItem>
        {subsystems.map((s) => (
          <WorkbenchNavItem key={s.key} asChild active={active === s.key}>
            <Link to={`/timeline?subsystem=${s.key}`}>{s.label}</Link>
          </WorkbenchNavItem>
        ))}
      </nav>
    </WorkbenchRail>
  )
}

export function TimelinePage() {
  const [searchParams] = useSearchParams()
  const subsystem = searchParams.get('subsystem')

  const [events, setEvents] = useState<TimelineEvent[]>([])
  const [subsystems, setSubsystems] = useState<TimelineSubsystem[]>([])
  const [nextBefore, setNextBefore] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [moreError, setMoreError] = useState<string | null>(null)

  const labelOf = useCallback(
    (key: string) => subsystems.find((s) => s.key === key)?.label ?? key,
    [subsystems],
  )

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await listTimeline({ subsystem: subsystem || undefined, limit: PAGE_SIZE })
      setEvents(data.events)
      setSubsystems(data.subsystems)
      setNextBefore(data.next_before)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load activity')
    } finally {
      setLoading(false)
    }
  }, [subsystem])

  useEffect(() => {
    void load()
  }, [load])

  async function loadMore() {
    if (!nextBefore) return
    setLoadingMore(true)
    setMoreError(null)
    try {
      const data = await listTimeline({
        subsystem: subsystem || undefined,
        limit: PAGE_SIZE,
        before: nextBefore,
      })
      setEvents((prev) => [...prev, ...data.events])
      setNextBefore(data.next_before)
    } catch (e) {
      setMoreError(e instanceof Error ? e.message : 'Failed to load more')
    } finally {
      setLoadingMore(false)
    }
  }

  const groups = groupByDay(events)

  return (
    <WorkbenchShell>
      <TimelineRail subsystems={subsystems} active={subsystem} />
      <WorkbenchMain>
        <div className="mx-auto max-w-3xl">
          {loading ? (
            <LoadingSpinner label="Loading activity…" />
          ) : error ? (
            <div className="p-4">
              <ErrorState message={error} onRetry={() => void load()} />
            </div>
          ) : events.length === 0 ? (
            <EmptyState
              title="No activity yet"
              description={
                subsystem ? 'Nothing in this subsystem yet.' : 'Activity will show up here as work happens.'
              }
            />
          ) : (
            <>
              {groups.map((g) => (
                <section key={g.day}>
                  <div className="sticky top-0 z-10 bg-background/95 px-4 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground backdrop-blur">
                    {g.day}
                  </div>
                  {g.events.map((ev) => (
                    <EventRow key={ev.id} ev={ev} label={labelOf(ev.subsystem)} />
                  ))}
                </section>
              ))}
              <div className="flex flex-col items-center gap-2 p-4">
                {nextBefore ? (
                  <button
                    type="button"
                    onClick={() => void loadMore()}
                    disabled={loadingMore}
                    className={clsx(
                      'rounded-md border border-border px-3 py-1.5 text-[13px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground',
                      loadingMore && 'opacity-60',
                    )}
                  >
                    {loadingMore ? 'Loading…' : moreError ? 'Retry' : 'Show more'}
                  </button>
                ) : (
                  <span className="text-[11px] text-muted-foreground">End of recent activity</span>
                )}
                {moreError && <span className="text-[11px] text-destructive">{moreError}</span>}
              </div>
            </>
          )}
        </div>
      </WorkbenchMain>
    </WorkbenchShell>
  )
}
