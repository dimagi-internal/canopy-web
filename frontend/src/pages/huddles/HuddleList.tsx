import { Link } from 'react-router-dom'
import type { HuddleSummary } from '@/api/huddles'
import { relativeTime } from '@/components/activity/turnLog'
import { MemberAvatar } from './HuddleGrid'
import { memberHue, roundName, roundsToShow } from './huddleModel'

/** The huddles index, one card per huddle — shared by /w/:ws/huddles and an
 * agent's Huddles section. */
export function HuddleList({ workspace, huddles, now = new Date() }: {
  workspace: string
  huddles: HuddleSummary[]
  now?: Date
}) {
  return (
    <ul className="flex flex-col gap-3">
      {huddles.map((h) => {
        const rounds = roundsToShow({ ...h, cells: [] })
        return (
          <li key={h.id}>
            <Link
              to={`/w/${workspace}/huddles/${encodeURIComponent(h.id)}`}
              className="group grid gap-3 rounded-xl border border-border bg-card p-4 transition-colors hover:border-primary/40 sm:grid-cols-[1fr_auto]"
            >
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-[14px] font-semibold text-foreground group-hover:text-primary">{h.id}</span>
                  {h.finished ? (
                    <span className="inline-flex h-5 items-center rounded-full border border-success/30 bg-success/10 px-2 text-[11px] font-medium text-success">
                      filed
                    </span>
                  ) : (
                    <span className="inline-flex h-5 items-center gap-1.5 rounded-full border border-info/30 bg-info/10 px-2 text-[11px] font-medium text-info">
                      <span className="size-1.5 animate-pulse rounded-full bg-info" />
                      in flight
                    </span>
                  )}
                </div>
                <p className="mt-1 text-[12px] text-muted-foreground">
                  {h.type || 'huddle'} · team {h.team || '—'} · led by <span className="text-foreground-secondary">{h.leader}</span>
                  {' · '}{relativeTime(h.created_at, now)}
                </p>
                <div className="mt-3 flex items-center gap-1" aria-label={`${h.members.length} members`}>
                  {h.members.map((m, i) => (
                    <span key={m} title={m} className="-ml-1 first:ml-0">
                      <MemberAvatar slug={m} hue={memberHue(i)} size="sm" />
                    </span>
                  ))}
                  <span className="ml-2 text-[11px] text-muted-foreground">{h.members.join(', ')}</span>
                </div>
              </div>
              <div className="flex items-end gap-5 sm:flex-col sm:items-end sm:justify-between">
                <ol className="flex items-center gap-1" aria-label={`${h.rounds_dispatched} of ${rounds.length} rounds dispatched`}>
                  {rounds.map((r) => (
                    <li
                      key={r}
                      title={`Round ${r} — ${roundName(h.type, r)}`}
                      className={
                        'h-1.5 w-7 rounded-full ' +
                        (r < h.rounds_dispatched || (r === h.rounds_dispatched && h.finished)
                          ? 'bg-primary'
                          : r === h.rounds_dispatched
                            ? 'bg-primary/50'
                            : 'bg-muted')
                      }
                    />
                  ))}
                </ol>
                <div className="text-right">
                  <div className="text-[18px] font-semibold leading-none text-foreground">{h.outcome_count}</div>
                  <div className="text-[11px] text-muted-foreground">{h.outcome_count === 1 ? 'outcome' : 'outcomes'}</div>
                </div>
              </div>
            </Link>
          </li>
        )
      })}
    </ul>
  )
}
