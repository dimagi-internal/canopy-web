import { useEffect, useState, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Button } from 'canopy-ui/ui'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import {
  approveAccessRequest,
  denyAccessRequest,
  getAccessRequest,
  removeMember,
  setMemberRole,
  WorkspaceApiError,
  type AccessRequestOut,
  type ApprovableRole,
  type MemberRole,
} from '@/api/workspaces'
import { grantableRoles, mayManageMember, roleAllows } from '@/lib/workspaceRoles'
import { approvableRoles, decisionText, emailDomain, notifyProblem, requesterLabel } from './accessRequests'

/**
 * One access request — the page the admins' email links to
 * (`/w/:workspace/settings/access-requests/:requestId`).
 *
 * Pending: who asked, their address and domain, their note, when — then a role
 * picker (only roles you may grant; viewer by default) with Approve, and Deny
 * with an optional reason the requester is emailed. Decided: the decision,
 * read-only. If it was approved and they are still here, their CURRENT role can
 * be changed or they can be removed — which is how an auto-approved request is
 * corrected.
 */
export function AccessRequestPage(): JSX.Element | null {
  const { workspace: slug, requestId } = useParams()
  const id = Number(requestId)
  const { workspaces } = useWorkspace()
  const myRole = workspaces.find((w) => w.slug === slug)?.role
  const canManage = roleAllows(myRole, 'members.manage')
  const roles = approvableRoles(myRole)

  const [req, setReq] = useState<AccessRequestOut | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [role, setRole] = useState<ApprovableRole>('viewer')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState<'approve' | 'deny' | 'member' | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  // Set once THIS visit decided the request, so the outcome reads as news.
  const [justDecided, setJustDecided] = useState(false)
  const [memberNote, setMemberNote] = useState<string | null>(null)

  useEffect(() => {
    if (!slug || !Number.isFinite(id) || !canManage) return
    let cancelled = false
    getAccessRequest(slug, id)
      .then((r) => {
        if (!cancelled) setReq(r)
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setLoadError(
          e instanceof WorkspaceApiError && e.status === 404
            ? 'No such access request in this workspace.'
            : e instanceof Error ? e.message : 'Failed to load the request',
        )
      })
    return () => {
      cancelled = true
    }
  }, [slug, id, canManage])

  if (!slug) return null
  const back = (
    <Link to=".." relative="path" className="text-[13px] text-primary hover:underline">
      ← All access requests
    </Link>
  )
  if (!canManage) {
    return (
      <div className="max-w-2xl space-y-3">
        {back}
        <p className="text-[13px] text-muted-foreground">
          Only this workspace&apos;s admins and owners decide access requests.
        </p>
      </div>
    )
  }
  if (loadError) {
    return (
      <div className="max-w-2xl space-y-3">
        {back}
        <p className="text-sm text-destructive">{loadError}</p>
      </div>
    )
  }
  if (!req) return <div className="h-32 max-w-2xl animate-pulse rounded-lg bg-muted" />

  async function decide(kind: 'approve' | 'deny') {
    if (!slug || !req) return
    setBusy(kind)
    setActionError(null)
    try {
      const next = kind === 'approve'
        ? await approveAccessRequest(slug, req.id, role)
        : await denyAccessRequest(slug, req.id, reason.trim())
      setReq(next)
      setJustDecided(true)
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Something went wrong')
    } finally {
      setBusy(null)
    }
  }

  async function changeMember(next: MemberRole | 'remove') {
    if (!slug || !req) return
    setBusy('member')
    setActionError(null)
    setMemberNote(null)
    try {
      if (next === 'remove') {
        await removeMember(slug, req.user_id)
        setReq({ ...req, current_role: null })
        setMemberNote(`${req.email} was removed from the workspace.`)
      } else {
        const m = await setMemberRole(slug, req.user_id, next)
        setReq({ ...req, current_role: m.role })
        setMemberNote(`${req.email} is now ${m.role}.`)
      }
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Something went wrong')
    } finally {
      setBusy(null)
    }
  }

  const pending = req.status === 'pending'
  const problem = notifyProblem(req)
  const current = req.current_role ?? null
  const mayChangeMember = current !== null && mayManageMember(myRole, current)

  return (
    <div className="max-w-2xl space-y-5">
      {back}

      <section className="rounded-lg border border-border bg-card p-5" aria-label="The request">
        <h2 className="text-sm font-semibold text-foreground">
          {requesterLabel(req)} requested an invitation
        </h2>
        <dl className="mt-3 grid gap-1 text-[13px] text-foreground-secondary sm:grid-cols-[7rem_1fr]">
          <dt className="font-medium text-foreground">Email</dt>
          <dd className="m-0">{req.email}</dd>
          <dt className="font-medium text-foreground">Domain</dt>
          <dd className="m-0">{emailDomain(req.email)}</dd>
          <dt className="font-medium text-foreground">Asked</dt>
          <dd className="m-0">{new Date(req.created_at).toLocaleString()}</dd>
          <dt className="font-medium text-foreground">Note</dt>
          <dd className="m-0 whitespace-pre-wrap">{req.note || <span className="text-muted-foreground">none</span>}</dd>
        </dl>
        {problem && <p className="mt-3 text-[12px] text-warning">{problem}</p>}
      </section>

      {actionError && <p className="text-sm text-destructive">{actionError}</p>}

      {pending ? (
        <section className="rounded-lg border border-border bg-card p-5" aria-label="Decide">
          <h2 className="text-sm font-semibold text-foreground">Approve</h2>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <label htmlFor="approve-role" className="text-[13px] text-foreground-secondary">
              Let them in as
            </label>
            <select
              id="approve-role"
              value={role}
              onChange={(e) => setRole(e.target.value as ApprovableRole)}
              className="rounded border border-input bg-input px-2 py-1 text-[13px] capitalize text-foreground"
            >
              {roles.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
            <Button type="button" size="sm" disabled={busy !== null} onClick={() => void decide('approve')}>
              {busy === 'approve' ? 'Approving…' : 'Approve'}
            </Button>
          </div>
          <p className="mt-1 text-[12px] text-muted-foreground">
            You can grant {roles.join(', ')}. They are emailed a link into the workspace.
          </p>

          <h2 className="mt-5 text-sm font-semibold text-foreground">Deny</h2>
          <textarea
            aria-label="Reason (optional, emailed to them)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            maxLength={1000}
            rows={2}
            placeholder="Reason (optional) — they are emailed it"
            className="mt-2 w-full rounded border border-input bg-input px-2 py-1.5 text-[13px] text-foreground"
          />
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={busy !== null}
            onClick={() => void decide('deny')}
            className="mt-2"
          >
            {busy === 'deny' ? 'Denying…' : 'Deny'}
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
          <h2 className="text-sm font-semibold text-foreground">
            {justDecided ? 'Done' : 'Decided'}
          </h2>
          <p className="mt-1 text-[13px] text-foreground-secondary">{decisionText(req)}</p>
          {req.status === 'denied' && req.decision_reason && (
            <p className="mt-1 text-[13px] text-foreground-secondary">Reason: “{req.decision_reason}”</p>
          )}
          {justDecided && (
            <p className="mt-1 text-[12px] text-muted-foreground">{req.email} has been emailed.</p>
          )}

          {req.status === 'approved' && (
            <div className="mt-4 border-t border-border pt-4">
              <h3 className="text-[13px] font-semibold text-foreground">Their membership now</h3>
              {current === null ? (
                <p className="mt-1 text-[13px] text-muted-foreground">No longer a member.</p>
              ) : mayChangeMember ? (
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <label htmlFor="member-role" className="text-[13px] text-foreground-secondary">
                    Role
                  </label>
                  <select
                    id="member-role"
                    value={current}
                    disabled={busy !== null}
                    onChange={(e) => void changeMember(e.target.value as MemberRole)}
                    className="rounded border border-input bg-input px-2 py-1 text-[13px] capitalize text-foreground"
                  >
                    {grantableRoles(myRole).map((r) => (
                      <option key={r} value={r}>
                        {r}
                      </option>
                    ))}
                  </select>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={busy !== null}
                    onClick={() => void changeMember('remove')}
                  >
                    Remove from workspace
                  </Button>
                </div>
              ) : (
                <p className="mt-1 text-[13px] capitalize text-muted-foreground">{current}</p>
              )}
              {memberNote && <p className="mt-2 text-[12px] text-foreground-secondary">{memberNote}</p>}
            </div>
          )}
          <div className="mt-4">{back}</div>
        </section>
      )}
    </div>
  )
}
