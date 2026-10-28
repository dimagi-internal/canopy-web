import { useState, type JSX } from 'react'
import { Link } from 'react-router-dom'
import { listMyActAsRequests, type ActAsRequest } from '@/api/actAs'
import { usePolled } from '@/hooks/usePolled'

// Agents asking to act as you on canopy, at the top of the feed — the push's twin, for
// when the phone never showed it. A request leaves this list when you decide, when the
// agent stops it, or when it lapses (10 minutes).

const POLL_MS = 15_000

export function ActAsRequests(): JSX.Element | null {
  const [rows, setRows] = useState<ActAsRequest[]>([])
  usePolled(
    (signal) =>
      listMyActAsRequests()
        .then((r) => {
          if (!signal.aborted) setRows(r)
        })
        .catch(() => {}),
    { ms: POLL_MS },
  )
  const pending = rows.filter((r) => r.status === 'pending')
  if (pending.length === 0) return null
  return (
    <div className="flex flex-col gap-2" data-testid="act-as-requests">
      {pending.map((r) => (
        <div
          key={r.id}
          role="alert"
          data-testid="act-as-request"
          className="rounded-lg border-2 border-warning bg-warning/10 p-3 text-foreground"
        >
          <p className="text-[13px] font-semibold">{r.agent_name || r.agent} asks to act as you on canopy</p>
          <p className="mt-0.5 text-[13px] leading-snug text-foreground-secondary">{r.reason}</p>
          <div className="mt-2">
            <Link
              to={`/act-as/${r.id}`}
              data-testid="act-as-open"
              className="rounded-md bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90"
            >
              Decide
            </Link>
          </div>
        </div>
      ))}
    </div>
  )
}
