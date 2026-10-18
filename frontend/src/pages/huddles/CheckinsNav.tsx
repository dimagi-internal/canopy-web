import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { listCheckins, type Checkin } from '@/api/checkins'

/**
 * Check-ins across projects, on the Huddles page — a navigation view only. A
 * check-in is a project's recurring meeting (a cadence, a lead, agents, maybe a
 * Slack channel); its record — settings, runs, what it proposed — lives on its
 * project's page, so each row links there.
 */
export function CheckinsNav({ workspace }: { workspace: string }) {
  const [rows, setRows] = useState<Checkin[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    listCheckins()
      .then((r) => alive && setRows(r))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      alive = false
    }
  }, [workspace])

  return (
    <section aria-label="Check-ins" className="mb-10">
      <h2 className="mb-1 text-lg font-semibold">Check-ins</h2>
      <p className="mb-3 text-[13px] text-muted-foreground">
        Projects that meet on a cadence. Each one lives on its project&apos;s page — add or change one there. A
        check-in with a Slack channel asks its lead in the channel first; nothing there is read until they press
        Confirm.
      </p>
      {error && <p className="text-sm text-destructive">Couldn’t load check-ins: {error}</p>}
      {rows !== null && rows.length === 0 && (
        <p className="text-sm text-muted-foreground">No check-ins yet. Add one from a project&apos;s page.</p>
      )}
      {rows !== null && rows.length > 0 && (
        <ul className="flex flex-col gap-2">
          {rows.map((c) => (
            <li key={c.id} className="rounded-xl border border-border bg-card p-3">
              <Link to={`${c.url}#project-checkins`} className="font-medium text-foreground hover:text-primary">
                {c.project.name} — {c.name}
              </Link>
              {!c.enabled && <span className="ml-2 text-[11px] text-muted-foreground">(paused)</span>}
              <p className="text-[12px] text-muted-foreground">
                {c.channel_id ? `#${c.channel_name || c.channel_id}` : 'no channel'} · lead {c.lead_email || '—'} ·{' '}
                {(c.agents ?? []).join(', ')} · <code>{c.cron}</code> {c.timezone}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
