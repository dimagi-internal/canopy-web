import { useEffect, useMemo, useState, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Button } from 'canopy-ui/ui'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { WorkspaceApiError } from '@/api/workspaces'
import {
  declineBetaRequest,
  getBetaRequest,
  inviteBetaRequest,
  listBetaRequests,
  type BetaInviteRole,
  type BetaRequestOut,
} from '@/api/betaRequests'
import { roleAllows } from '@/lib/workspaceRoles'
import { approvableRoles } from './accessRequests'

const NOT_YOURS = 'Requests for access to Canopy are answered by the person they are emailed to.'

function loadErrorText(e: unknown): string {
  return e instanceof WorkspaceApiError && e.status === 404
    ? NOT_YOURS
    : e instanceof Error ? e.message : 'Failed to load'
}

function decisionText(req: BetaRequestOut): string {
  const by = req.decided_by ? ` by ${req.decided_by}` : ''
  const when = req.decided_at ? ` on ${new Date(req.decided_at).toLocaleString()}` : ''
  if (req.status === 'invited') {
    return `Invited to ${req.workspace_name ?? req.workspace ?? 'a workspace'} as ${req.role}${by}${when}.`
  }
  return `Declined${by}${when}.`
}

const EMAIL_STATUS: Record<string, string> = {
  sent: 'They have been emailed the invite link.',
  throttled: 'The invite was emailed under a minute ago, so it was not sent again.',
  not_configured: 'Email delivery is off here — copy the invite link from the workspace’s Members page.',
  failed: 'The invite exists but the email failed — copy its link from the workspace’s Members page.',
}

/**
 * Requests for access to Canopy from the public site's form (`/beta-requests`).
 * Only their reviewer (the address they are emailed to, or a superuser) sees
 * them; anyone else gets a 404 and a sentence saying whose they are.
 */
export function BetaRequestsPage(): JSX.Element {
  const [reqs, setReqs] = useState<BetaRequestOut[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    listBetaRequests().then(setReqs).catch((e: unknown) => setError(loadErrorText(e)))
  }, [])

  if (error) return <p className="max-w-2xl text-[13px] text-muted-foreground">{error}</p>
  if (!reqs) return <div className="h-32 max-w-2xl animate-pulse rounded-lg bg-muted" />
  return (
    <div className="max-w-2xl space-y-3">
      <h1 className="text-base font-semibold text-foreground">Requests for access to Canopy</h1>
      {reqs.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">None yet.</p>
      ) : (
        <ul className="divide-y divide-border rounded-lg border border-border bg-card">
          {reqs.map((r) => (
            <li key={r.id}>
              <Link to={`/beta-requests/${r.id}`} className="flex items-center justify-between gap-3 px-4 py-2.5 hover:bg-muted/50">
                <span className="text-[13px] text-foreground">{r.email}</span>
                <span className="text-[12px] capitalize text-muted-foreground">
                  {r.status} · {new Date(r.created_at).toLocaleDateString()}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/**
 * One request — the page the notification email links to
 * (`/beta-requests/:requestId`). Pending: who asked and why, then a workspace
 * (only those where you can invite) and a role (only roles you may grant there,
 * viewer by default) — Approve sends the ordinary workspace invite. Decline
 * closes it and emails nobody. Answered: the answer, read-only.
 */
export function BetaRequestPage(): JSX.Element {
  const { requestId } = useParams()
  const id = Number(requestId)
  const { workspaces } = useWorkspace()
  const targets = useMemo(
    () => workspaces.filter((w) => roleAllows(w.role, 'members.manage')),
    [workspaces],
  )

  const [req, setReq] = useState<BetaRequestOut | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [slug, setSlug] = useState('')
  const [role, setRole] = useState<BetaInviteRole>('viewer')
  const [busy, setBusy] = useState<'invite' | 'decline' | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [justDecided, setJustDecided] = useState(false)

  useEffect(() => {
    if (!Number.isFinite(id)) return
    getBetaRequest(id).then(setReq).catch((e: unknown) => setLoadError(loadErrorText(e)))
  }, [id])

  const chosen = targets.find((w) => w.slug === slug) ?? targets[0]
  const roles = approvableRoles(chosen?.role)
  // Keep the role valid when the workspace changes to one where you grant less.
  const effectiveRole: BetaInviteRole = roles.includes(role) ? role : 'viewer'

  const back = (
    <Link to="/beta-requests" className="text-[13px] text-primary hover:underline">
      ← All requests
    </Link>
  )
  if (loadError) {
    return (
      <div className="max-w-2xl space-y-3">
        <p className="text-[13px] text-muted-foreground">{loadError}</p>
      </div>
    )
  }
  if (!req) return <div className="h-32 max-w-2xl animate-pulse rounded-lg bg-muted" />

  async function decide(kind: 'invite' | 'decline') {
    if (!req) return
    setBusy(kind)
    setActionError(null)
    try {
      setReq(kind === 'invite' && chosen
        ? await inviteBetaRequest(req.id, chosen.slug, effectiveRole)
        : await declineBetaRequest(req.id))
      setJustDecided(true)
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Something went wrong')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="max-w-2xl space-y-5">
      {back}

      <section className="rounded-lg border border-border bg-card p-5" aria-label="The request">
        <h2 className="text-sm font-semibold text-foreground">{req.email} asked for access to Canopy</h2>
        <dl className="mt-3 grid gap-1 text-[13px] text-foreground-secondary sm:grid-cols-[7rem_1fr]">
          <dt className="font-medium text-foreground">Asked</dt>
          <dd className="m-0">{new Date(req.created_at).toLocaleString()}</dd>
          <dt className="font-medium text-foreground">Why</dt>
          <dd className="m-0 whitespace-pre-wrap">{req.reason}</dd>
        </dl>
      </section>

      {actionError && <p className="text-sm text-destructive">{actionError}</p>}

      {req.status === 'pending' ? (
        <section className="rounded-lg border border-border bg-card p-5" aria-label="Decide">
          <h2 className="text-sm font-semibold text-foreground">Approve</h2>
          {targets.length === 0 ? (
            <p className="mt-1 text-[13px] text-muted-foreground">
              You are not an admin or owner of any workspace, so there is nowhere you can invite them.
            </p>
          ) : (
            <>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <label htmlFor="beta-workspace" className="text-[13px] text-foreground-secondary">
                  Invite them to
                </label>
                <select
                  id="beta-workspace"
                  value={chosen?.slug}
                  onChange={(e) => setSlug(e.target.value)}
                  className="rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground"
                >
                  {targets.map((w) => (
                    <option key={w.slug} value={w.slug}>
                      {w.display_name}
                    </option>
                  ))}
                </select>
                <label htmlFor="beta-role" className="text-[13px] text-foreground-secondary">
                  as
                </label>
                <select
                  id="beta-role"
                  value={effectiveRole}
                  onChange={(e) => setRole(e.target.value as BetaInviteRole)}
                  className="rounded border border-input bg-input px-2 py-1 text-[13px] capitalize text-foreground"
                >
                  {roles.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
                <Button type="button" size="sm" disabled={busy !== null} onClick={() => void decide('invite')}>
                  {busy === 'invite' ? 'Inviting…' : 'Approve and invite'}
                </Button>
              </div>
              <p className="mt-1 text-[12px] text-muted-foreground">
                Canopy emails {req.email} an invite link; signing in with that address lets them in.
              </p>
            </>
          )}

          <h2 className="mt-5 text-sm font-semibold text-foreground">Decline</h2>
          <p className="mt-1 text-[12px] text-muted-foreground">
            Closes the request. Nobody is emailed — reply to the notification email to tell them why.
          </p>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={busy !== null}
            onClick={() => void decide('decline')}
            className="mt-2"
          >
            {busy === 'decline' ? 'Declining…' : 'Decline'}
          </Button>
        </section>
      ) : (
        <section
          className={
            justDecided
              ? 'rounded-lg border border-success/30 bg-success/5 p-5'
              : 'rounded-lg border border-border bg-card p-5'
          }
          aria-label="Decision"
        >
          <h2 className="text-sm font-semibold text-foreground">{justDecided ? 'Done' : 'Answered'}</h2>
          <p className="mt-1 text-[13px] text-foreground-secondary">{decisionText(req)}</p>
          {req.email_status && (
            <p className="mt-1 text-[12px] text-muted-foreground">{EMAIL_STATUS[req.email_status] ?? ''}</p>
          )}
          {req.status === 'invited' && req.workspace && (
            <p className="mt-2 text-[13px]">
              <Link to={`/w/${req.workspace}/settings/members`} className="text-primary hover:underline">
                See the invite in Members →
              </Link>
            </p>
          )}
        </section>
      )}
    </div>
  )
}
