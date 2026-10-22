import { useState, type JSX } from 'react'
import { listMySignInRequests, type OpenSignInRequest } from '@/api/harness'
import { usePolled } from '@/hooks/usePolled'

// Sign-ins a box is waiting on you to approve (AWS SSO's device code), at the
// top of the feed. The push notification carries the same link, but the push
// service accepting a push is not the phone showing it: on 2026-10-19 three
// were accepted and never displayed, and nothing else said a box was waiting.
// A request leaves this list when its box reports the outcome or its code
// expires (about ten minutes), so re-reading is what clears a card.

const POLL_MS = 15_000

function minutesLeft(expiresAt: string, now: number): number {
  return Math.max(0, Math.ceil((new Date(expiresAt).getTime() - now) / 60_000))
}

export function SignInRequests(): JSX.Element | null {
  const [rows, setRows] = useState<OpenSignInRequest[]>([])
  const [now, setNow] = useState(() => Date.now())

  // A failed poll is non-fatal: the push is the primary channel. Coming back
  // from the AWS tab (visible again) is when an approval has just happened, and
  // usePolled refetches then.
  usePolled(
    (signal) =>
      listMySignInRequests()
        .then((r) => {
          if (signal.aborted) return
          setRows(r)
          setNow(Date.now())
        })
        .catch(() => {}),
    { ms: POLL_MS },
  )

  const open = rows.filter((r) => minutesLeft(r.expires_at, now) > 0)
  if (open.length === 0) return null
  return (
    <div className="flex flex-col gap-2" data-testid="sign-in-requests">
      {open.map((r) => {
        const what = r.label ? `AWS sign-in (${r.label})` : 'AWS sign-in'
        const left = minutesLeft(r.expires_at, now)
        return (
          <div
            key={r.id}
            role="alert"
            data-testid="sign-in-request"
            className="rounded-lg border-2 border-warning bg-warning/10 p-3 text-foreground"
          >
            <p className="text-[13px] font-semibold">
              {r.requested_by || 'An agent'} needs {what}
            </p>
            <p className="mt-0.5 text-[13px] leading-snug text-foreground-secondary">
              On {r.runner_name}: {r.reason}
            </p>
            <div className="mt-2 flex items-center gap-3">
              <a
                href={r.url}
                target="_blank"
                rel="noopener noreferrer"
                data-testid="sign-in-approve"
                className="rounded-md bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90"
              >
                Approve on AWS
              </a>
              <span className="text-[12px] text-muted-foreground">
                Expires in about {left} min
              </span>
            </div>
          </div>
        )
      })}
    </div>
  )
}
