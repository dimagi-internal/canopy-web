import { useCallback, useEffect, useRef, useState, type JSX } from 'react'
import {
  grantRunnerAdmin,
  listRunnerAdmins,
  revokeRunnerAdmin,
  type RunnerAdmin,
} from '@/api/harness'

// Who may administer this box, and the form to say so.
//
// `GET/POST/DELETE /runners/{id}/admins` shipped with the grant itself and had no
// page, so the only way to add someone was a curl with the pairer's token — which
// is exactly the shape of thing that does not happen, and then a box has one
// human who can fix it. The grant exists because that single point of failure
// already bit once (2026-09-08, a signed-out cloud runner).
//
// Two tiers, mirrored here rather than guessed at:
//   * LISTING is open to anyone who already administers the box. A non-admin gets
//     a 404 from the server, which this renders as "you can't see this", never as
//     "nobody administers it" — an empty list and a refused read must not look
//     the same.
//   * GRANTING and REVOKING stay with the PAIRER (`can_manage`), so a grantee
//     cannot mint more grantees. The form is simply absent otherwise.
export function RunnerAdmins({
  runnerId,
  canManage,
  pairedByEmail,
}: {
  runnerId: string
  /** Is the viewer the pairer — the only tier that may grant. */
  canManage: boolean
  pairedByEmail?: string | null
}): JSX.Element {
  const [admins, setAdmins] = useState<RunnerAdmin[] | null>(null)
  const [refused, setRefused] = useState(false)
  const [email, setEmail] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])

  const load = useCallback(() => {
    listRunnerAdmins(runnerId)
      .then((rows) => { if (alive.current) { setAdmins(rows); setRefused(false) } })
      .catch(() => { if (alive.current) { setAdmins([]); setRefused(true) } })
  }, [runnerId])

  useEffect(load, [load])

  const grant = () => {
    const value = email.trim()
    if (!value) return
    setBusy(true)
    setError(null)
    grantRunnerAdmin(runnerId, value)
      .then(() => { if (alive.current) { setEmail(''); load() } })
      // The server's own words: "no account with email …" and "not a member of the
      // workspace this runner belongs to" are the two real failures, and both are
      // fixable by the person reading them. Paraphrasing would lose that.
      .catch((e: unknown) => { if (alive.current) setError(e instanceof Error ? e.message : 'Failed to grant') })
      .finally(() => { if (alive.current) setBusy(false) })
  }

  const revoke = (admin: RunnerAdmin) => {
    setBusy(true)
    setError(null)
    revokeRunnerAdmin(runnerId, admin.user_id)
      .then(() => { if (alive.current) load() })
      .catch((e: unknown) => { if (alive.current) setError(e instanceof Error ? e.message : 'Failed to revoke') })
      .finally(() => { if (alive.current) setBusy(false) })
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3" data-testid="runner-admins">
      <div className="flex items-center gap-2">
        <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Administrators</span>
        {pairedByEmail && (
          <span className="ml-auto text-[11px] text-foreground-subtle">paired by {pairedByEmail}</span>
        )}
      </div>

      {refused ? (
        <p className="text-[12px] text-muted-foreground" data-testid="runner-admins-refused">
          Only someone who already administers this box can see its list.
        </p>
      ) : admins === null ? (
        <div className="h-6 w-40 animate-pulse rounded-md bg-muted" data-testid="runner-admins-loading" />
      ) : admins.length === 0 ? (
        <p className="text-[12px] text-muted-foreground" data-testid="runner-admins-empty">
          Nobody but the pairer. If they lose access to this box, nobody can re-authenticate it.
        </p>
      ) : (
        <ul className="flex flex-col gap-1">
          {admins.map((a) => (
            <li key={a.user_id} className="flex items-center gap-2 text-[12px] text-foreground" data-testid={`runner-admin-${a.user_id}`}>
              <span>{a.email}</span>
              {a.granted_by_email && (
                <span className="text-[11px] text-foreground-subtle">granted by {a.granted_by_email}</span>
              )}
              {canManage && (
                <button
                  type="button"
                  onClick={() => revoke(a)}
                  disabled={busy}
                  aria-label={`Revoke ${a.email}`}
                  className="ml-auto rounded-md border border-border px-1.5 py-0.5 text-[11px] text-muted-foreground disabled:opacity-40"
                >
                  Revoke
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {canManage && (
        <div className="flex items-center gap-2">
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') grant() }}
            placeholder="colleague@dimagi.com"
            aria-label="Grant administration to"
            className="min-w-0 flex-1 rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground placeholder:text-muted-foreground"
          />
          <button
            type="button"
            onClick={grant}
            disabled={busy || !email.trim()}
            data-testid="runner-admins-grant"
            className="rounded-md bg-primary px-2.5 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40"
          >
            {busy ? 'Saving…' : 'Add'}
          </button>
        </div>
      )}

      {error && <p className="text-[12px] text-destructive" data-testid="runner-admins-error">{error}</p>}

      <p className="text-[11px] text-foreground-subtle">
        An administrator can set this box's credentials, sign it back in, and send work to it — on a
        cloud runner, that is what lets someone move their own queued work here when their laptop is
        offline. They cannot add or remove administrators; only the pairer can.
      </p>
    </div>
  )
}
