import { useCallback, useEffect, useRef, useState, type JSX } from 'react'
import { Link } from 'react-router-dom'
import clsx from 'clsx'

import { actOnAlert, loadAlerts, undismissAlert, type Alert, type AlertAction, type AlertSeverity } from '@/api/alerts'
import { ApiError } from '@/api/problem'
import { usePolled } from '@/hooks/usePolled'
import { relativeAge } from '@/lib/relativeAge'

// ALERTS — the one component every screen shows alerts with (Home, Topology,
// the agent page, a runner's detail). A screen never decides what to show: it
// names a scope and gets the open alerts there that the viewer has an action on,
// in the server's order (blocking, action, setup; most recently raised first).
//
// Compact on purpose: one line per alert — severity dot · source chip · title ·
// its actions inline · ×. The × is the source's shared dismiss ("Dismiss for
// everyone"): it clears the alert for everybody and is logged, so it is not a
// personal hide, and there is no "quiet" fold — an open alert stays until the
// thing underneath is fixed or someone dismisses it. → docs/architecture/alerts.md

/** Source chip text per alert kind; an unknown kind shows its raw name. */
export const KIND_LABEL: Record<string, string> = {
  runner_credential: 'Runner credential',
  runner_code: 'Runner code',
  turn_failure: 'Failed turns',
  unclaimable_turns: 'Stuck turns',
  device_key_pending: 'Device key',
  agent_cant_run: 'Agent routing',
  canopy_projects_off: 'Canopy projects',
}

const SEVERITIES: { id: AlertSeverity; label: string; dot: string }[] = [
  { id: 'blocking', label: 'Blocking', dot: 'bg-destructive' },
  { id: 'action', label: 'Action', dot: 'bg-warning' },
  { id: 'setup', label: 'Setup', dot: 'bg-muted-foreground' },
]
const DOT: Record<AlertSeverity, string> = Object.fromEntries(SEVERITIES.map((s) => [s.id, s.dot])) as Record<
  AlertSeverity,
  string
>

type Lists = { open: Alert[]; dismissed: Alert[] }

export function AlertList({
  scope,
  hideEmpty = false,
  showCounts = false,
  showDismissed = false,
  title,
  className,
}: {
  /** `workspace:<slug>`, `runner:<id>` or `agent:<slug>`; none = everything you can act on. */
  scope?: string
  /** Render nothing at all while there is nothing to show (no empty state, no heading). */
  hideEmpty?: boolean
  /** Severity count chips above the list, which filter it. */
  showCounts?: boolean
  /** A collapsed "Dismissed (n)" fold: who dismissed each one, when, and Undo. */
  showDismissed?: boolean
  /** A small heading with the open count, for a screen that gives alerts their own section. */
  title?: string
  className?: string
}): JSX.Element | null {
  const [filter, setFilter] = useState<AlertSeverity | null>(null)
  const [foldOpen, setFoldOpen] = useState(false)
  const [pending, setPending] = useState<Set<number>>(new Set())
  const [actError, setActError] = useState<string | null>(null)
  // An action's error survives the refresh that action itself triggers, and
  // clears on the next successful read after that.
  const keepError = useRef(0)
  const failAct = (message: string) => {
    keepError.current = 1
    setActError(message)
  }

  const fetchLists = useCallback(async (): Promise<Lists> => {
    const [open, dismissed] = await Promise.all([
      loadAlerts(scope, 'open'),
      showDismissed ? loadAlerts(scope, 'dismissed') : Promise.resolve([] as Alert[]),
    ])
    if (keepError.current > 0) keepError.current -= 1
    else setActError(null)
    return { open, dismissed }
  }, [scope, showDismissed])

  const { data, error, refresh, setData } = usePolled(fetchLists, { ms: 30_000, deps: [fetchLists] })

  // A severity filter whose last alert went away falls back to All, rather than
  // leaving an empty list behind a chip that is no longer there.
  useEffect(() => {
    if (filter && !(data?.open ?? []).some((a) => a.severity === filter)) setFilter(null)
  }, [data, filter])

  const busy = (id: number, on: boolean) =>
    setPending((prev) => {
      const next = new Set(prev)
      if (on) next.add(id)
      else next.delete(id)
      return next
    })

  async function run(alert: Alert, action: AlertAction) {
    setActError(null)
    busy(alert.id, true)
    // A closing action takes the row off at once; the next read confirms it.
    if (action.closes) setData((d) => d && { ...d, open: d.open.filter((a) => a.id !== alert.id) })
    try {
      await actOnAlert(alert.id, action.id)
    } catch (e) {
      failAct(
        e instanceof ApiError && e.code === 'action-not-offered'
          ? `“${action.label}” is no longer yours to do on that alert.`
          : e instanceof Error
            ? e.message
            : 'That did not work',
      )
    } finally {
      busy(alert.id, false)
      void refresh()
    }
  }

  async function undo(alert: Alert) {
    setActError(null)
    busy(alert.id, true)
    try {
      await undismissAlert(alert.id)
    } catch (e) {
      failAct(e instanceof Error ? e.message : 'Could not put it back')
    } finally {
      busy(alert.id, false)
      void refresh()
    }
  }

  const open = data?.open ?? []
  const dismissed = data?.dismissed ?? []
  if (hideEmpty && open.length === 0 && dismissed.length === 0) return null
  if (data === undefined && !error) return null

  const shown = filter ? open.filter((a) => a.severity === filter) : open
  const counts = SEVERITIES.map((s) => ({ ...s, n: open.filter((a) => a.severity === s.id).length })).filter((s) => s.n)

  return (
    <section className={clsx('flex flex-col gap-1.5', className)} data-testid="alert-list">
      {(title || (showCounts && open.length > 0)) && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          {title && (
            <h2 className="m-0 text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
              {title} <span className="tabular-nums">{open.length}</span>
            </h2>
          )}
          {showCounts && open.length > 0 && (
            <div className="flex flex-wrap gap-1 text-[11px]" role="group" aria-label="Severity">
              <CountChip label="All" n={open.length} on={filter === null} onClick={() => setFilter(null)} />
              {counts.map((s) => (
                <CountChip key={s.id} label={s.label} n={s.n} dot={s.dot} on={filter === s.id}
                  onClick={() => setFilter(filter === s.id ? null : s.id)} />
              ))}
            </div>
          )}
        </div>
      )}

      {error && !data && <p className="text-[12px] text-destructive">{error.message}</p>}
      {actError && <p className="text-[12px] text-destructive" role="alert">{actError}</p>}

      {open.length === 0 ? (
        !hideEmpty && <p className="text-[12px] text-muted-foreground">No alerts.</p>
      ) : (
        <div className="overflow-hidden rounded border border-border bg-background">
          {shown.map((a) => (
            <AlertRow key={a.id} alert={a} busy={pending.has(a.id)} onAct={(action) => void run(a, action)} />
          ))}
        </div>
      )}

      {showDismissed && dismissed.length > 0 && (
        <div>
          <button type="button" onClick={() => setFoldOpen((v) => !v)} aria-expanded={foldOpen}
            className="min-h-6 text-[11px] text-muted-foreground hover:text-foreground">
            <span aria-hidden className="mr-1 inline-block w-2">{foldOpen ? '▾' : '▸'}</span>
            Dismissed ({dismissed.length})
          </button>
          {foldOpen && (
            <div className="mt-1 overflow-hidden rounded border border-border bg-background">
              {dismissed.map((a) => (
                <div key={a.id} className="flex items-center gap-2 border-t border-border px-2.5 py-1 text-[12px] first:border-t-0">
                  <Dot severity={a.severity} />
                  <SourceChip kind={a.kind} />
                  <span className="min-w-0 flex-1 truncate text-muted-foreground">{a.title}</span>
                  {/* Who and when stay visible on a phone; the title gives way. */}
                  <span className="max-w-[45%] shrink-0 truncate text-[11px] text-muted-foreground"
                    title={a.closed_at ? new Date(a.closed_at).toLocaleString() : undefined}>
                    {a.closed_by_email || 'someone'}
                    {a.closed_at ? ` · ${relativeAge(a.closed_at, new Date(), { seconds: false })}` : ''}
                  </span>
                  <button type="button" disabled={pending.has(a.id)} onClick={() => void undo(a)}
                    aria-label={`Undo dismiss: ${a.title}`}
                    className="min-h-6 min-w-6 shrink-0 px-1 text-[11px] font-medium text-primary hover:underline disabled:opacity-50">
                    Undo
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  )
}

function AlertRow({ alert, busy, onAct }: { alert: Alert; busy: boolean; onAct: (a: AlertAction) => void }): JSX.Element {
  const dismiss = alert.actions.find((a) => a.id === 'dismiss')
  const actions = alert.actions.filter((a) => a.id !== 'dismiss')
  const tip = [alert.title, alert.body, alert.occurrences > 1 ? `${alert.occurrences} times` : '']
    .filter(Boolean)
    .join('\n\n')
  return (
    <div
      className="group flex flex-wrap items-center gap-x-2 gap-y-1 border-t border-border px-2.5 py-1 text-[13px] first:border-t-0 hover:bg-muted/50"
      data-testid="alert-row"
    >
      <Dot severity={alert.severity} />
      <SourceChip kind={alert.kind} />
      <span className="min-w-40 flex-1 truncate" title={tip}>
        {alert.url ? (
          <Link to={alert.url} className="text-foreground hover:text-primary">{alert.title}</Link>
        ) : (
          <span className="text-foreground">{alert.title}</span>
        )}
        {alert.last_notify && <span className="ml-2 text-[11px] text-muted-foreground">{alert.last_notify}</span>}
      </span>
      <span className="flex shrink-0 items-center gap-1">
        {actions.map((a) => {
          const inner = (
            <>
              {a.label}
              <span className={clsx('text-[10px]', a.closes ? 'text-success' : 'text-muted-foreground')}>
                {a.closes ? 'closes' : 'stays open'}
              </span>
            </>
          )
          const cls =
            'inline-flex min-h-6 items-center gap-1 rounded border border-border px-1.5 text-[12px] text-foreground-secondary hover:bg-muted disabled:opacity-50'
          if (a.href) {
            // Following a link is logged (fire-and-forget); navigation never waits on it.
            const follow = () => void actOnAlert(alert.id, a.id).catch(() => {})
            return /^https?:\/\//.test(a.href) ? (
              <a key={a.id} href={a.href} target="_blank" rel="noreferrer" onClick={follow} className={cls}>{inner}</a>
            ) : (
              <Link key={a.id} to={a.href} onClick={follow} className={cls}>{inner}</Link>
            )
          }
          return (
            <button key={a.id} type="button" disabled={busy} onClick={() => onAct(a)} className={cls}>{inner}</button>
          )
        })}
        {dismiss && (
          <button
            type="button"
            disabled={busy}
            onClick={() => onAct(dismiss)}
            aria-label="Dismiss for everyone"
            title="Dismiss for everyone. It is logged, and comes back only if something new happens."
            className="inline-flex min-h-6 min-w-6 items-center justify-center rounded text-[14px] leading-none text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-50"
          >
            ×
          </button>
        )}
      </span>
    </div>
  )
}

function Dot({ severity }: { severity: AlertSeverity }): JSX.Element {
  return <span role="img" aria-label={severity} title={severity} className={clsx('inline-block h-2 w-2 shrink-0 rounded-full', DOT[severity])} />
}

function SourceChip({ kind }: { kind: string }): JSX.Element {
  return (
    <span className="shrink-0 rounded bg-muted px-1.5 py-px text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
      {KIND_LABEL[kind] ?? kind}
    </span>
  )
}

function CountChip({ label, n, dot, on, onClick }: {
  label: string
  n: number
  dot?: string
  on: boolean
  onClick: () => void
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={on}
      className={clsx(
        'inline-flex min-h-6 items-center gap-1 rounded-full border px-2',
        on ? 'border-primary/30 bg-primary/10 font-medium text-primary' : 'border-border text-muted-foreground hover:text-foreground',
      )}
    >
      {dot && <span aria-hidden className={clsx('inline-block h-1.5 w-1.5 rounded-full', dot)} />}
      {label} <span className="tabular-nums">{n}</span>
    </button>
  )
}
