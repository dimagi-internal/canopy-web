import { useEffect, useState } from 'react'

import { addMySessionReviewer, listMySessionReviewers, removeMySessionReviewer } from '@/api/people'
import type { SessionReviewers as SessionReviewersData } from '@/api/people'

/** The words every coaching opt-in uses, so the first-run checkbox and this
 *  switch promise the same thing. */
export function coachLabel(name: string): string {
  return `Have ${name} coach me: it reviews my canopy sessions and sends short feedback; turn off any time`
}

/** "Who can review your sessions": the coach canopy offers (one switch), and any
 *  other agent you let read your sessions, each with Stop. Read-only grants,
 *  enforced by the session ACL (apps/canopy_sessions/access.py, leg 5). */
export function SessionReviewersPanel() {
  const [data, setData] = useState<SessionReviewersData | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    listMySessionReviewers()
      .then(setData)
      .catch(() => {
        // Only a person has this (an agent's login or a system account is
        // refused): a failed load just leaves the panel out.
      })
  }, []) // once, on mount

  const act = async (work: () => Promise<SessionReviewersData>) => {
    setBusy(true)
    setError(null)
    try {
      setData(await work())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (!data) return null
  const coach = data.coach?.member ? data.coach : null
  const others = data.reviewers.filter((r) => r.agent !== coach?.agent)
  if (!coach && others.length === 0) return null
  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-3">
      <h2 className="text-[10px] uppercase tracking-wider font-semibold text-muted-foreground">Who can review your sessions</h2>
      <p className="text-xs text-muted-foreground">
        An agent you let in reads the chats you start and the sessions on your own runners — never a
        private (sensitive) one — and changes nothing. Turning it on or off is on your memory audit.
      </p>
      {coach && (
        <label className="flex items-start gap-2 text-sm text-foreground">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={coach.on}
            disabled={busy}
            onChange={() =>
              void act(() => (coach.on ? removeMySessionReviewer(coach.agent) : addMySessionReviewer(coach.agent)))
            }
          />
          <span>{coachLabel(coach.name)}</span>
        </label>
      )}
      {others.length > 0 && (
        <ul className="space-y-2">
          {others.map((r) => (
            <li key={r.agent} className="flex items-center justify-between gap-3 text-sm">
              <span>
                <span className="font-medium text-foreground">{r.name}</span>{' '}
                <span className="text-xs text-muted-foreground">
                  since {new Date(r.granted_at).toLocaleDateString()}
                </span>
              </span>
              <button
                className="text-xs text-destructive hover:underline disabled:opacity-50"
                disabled={busy}
                onClick={() => void act(() => removeMySessionReviewer(r.agent))}
              >
                Stop
              </button>
            </li>
          ))}
        </ul>
      )}
      {error && <p role="alert" className="mt-2 text-xs text-destructive">{error}</p>}
    </div>
  )
}
