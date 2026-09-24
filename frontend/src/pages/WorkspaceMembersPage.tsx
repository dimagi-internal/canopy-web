import { useEffect, useMemo, useState, type FormEvent, type JSX } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Button, Input, Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from 'canopy-ui/ui'
import { WorkbenchSubHeader } from 'canopy-ui'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { useAuth } from '@/auth/AuthProvider'
import {
  createInvite,
  listInvites,
  listMembers,
  reissueInvite,
  removeMember,
  revokeInvite,
  setMemberRole,
  WorkspaceApiError,
  type InviteOut,
  type InviteRole,
  type MemberOut,
  type MemberRole,
} from '@/api/workspaces'
import { emailOutcomeText, inviteExpiryLabel, isInviteOutstanding, isInvitePending, type EmailStatus } from './workspaceInvites'

const ROLES: InviteRole[] = ['owner', 'editor', 'viewer']

// The absolute, copy-pasteable accept link — the same one the email carries,
// and the fallback whenever the email did not go out.
function inviteLink(token: string): string {
  const base = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')
  return `${window.location.origin}${base}/invite/${token}`
}

export function WorkspaceMembersPage(): JSX.Element | null {
  const { workspace: slug } = useParams()
  const navigate = useNavigate()
  const { workspaces, refresh: refreshWorkspaces } = useWorkspace()
  const isOwner = workspaces.find((w) => w.slug === slug)?.role === 'owner'
  const auth = useAuth()
  const myEmail = auth.status === 'authenticated' ? auth.user.email.toLowerCase() : null

  const [members, setMembers] = useState<MemberOut[] | null>(null)
  const [invites, setInvites] = useState<InviteOut[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [rowError, setRowError] = useState<string | null>(null)
  const [savingRoleFor, setSavingRoleFor] = useState<number | null>(null)

  const [email, setEmail] = useState('')
  const [role, setRole] = useState<InviteRole>('editor')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [created, setCreated] = useState<{ email: string; link: string; status: EmailStatus | null } | null>(null)
  const [copied, setCopied] = useState(false)
  // Per-row link actions on an outstanding invite: which row's link was just
  // copied, which is mid-reissue, and the fresh link a reissue produced (shown
  // on screen too, since the clipboard can be blocked).
  const [copiedInviteId, setCopiedInviteId] = useState<number | null>(null)
  const [reissuingId, setReissuingId] = useState<number | null>(null)
  const [reissued, setReissued] = useState<{ email: string; link: string; status: EmailStatus | null } | null>(null)

  useEffect(() => {
    if (!slug) return
    let cancelled = false
    setMembers(null)
    setInvites(null)
    setLoadError(null)
    Promise.all([listMembers(slug), listInvites(slug)])
      .then(([m, i]) => {
        if (cancelled) return
        setMembers(m)
        setInvites(i)
      })
      .catch((e: unknown) => {
        if (cancelled) return
        if (e instanceof WorkspaceApiError && e.status === 404) {
          // Non-member — the API 404s rather than leaking existence. Bounce
          // to the workspace home instead of showing a broken members page.
          navigate('/', { replace: true })
          return
        }
        setLoadError(e instanceof Error ? e.message : 'Failed to load members')
      })
    return () => {
      cancelled = true
    }
  }, [slug, navigate])

  const outstandingInvites = useMemo(() => (invites ?? []).filter(isInviteOutstanding), [invites])
  const ownerCount = useMemo(() => (members ?? []).filter((m) => m.role === 'owner').length, [members])

  async function handleRoleChange(userId: number, role: MemberRole) {
    if (!slug) return
    setRowError(null)
    setSavingRoleFor(userId)
    try {
      const updated = await setMemberRole(slug, userId, role)
      setMembers((prev) => (prev ?? []).map((m) => (m.user_id === userId ? updated : m)))
      // The cached workspace list (header switcher, this page's own `isOwner`)
      // is fetched once and otherwise never invalidated — if I just changed
      // MY OWN role, refresh it now so `isOwner` reflects reality immediately
      // instead of continuing to render owner-only controls I can no longer
      // use until a full page reload (see WorkspaceProvider.refresh's docstring).
      if (myEmail && updated.email.toLowerCase() === myEmail) {
        void refreshWorkspaces()
      }
    } catch (e) {
      setRowError(e instanceof Error ? e.message : 'Failed to change role')
    } finally {
      setSavingRoleFor(null)
    }
  }

  async function handleRemoveMember(userId: number) {
    if (!slug) return
    setRowError(null)
    try {
      await removeMember(slug, userId)
      setMembers((prev) => (prev ?? []).filter((m) => m.user_id !== userId))
    } catch (e) {
      setRowError(e instanceof Error ? e.message : 'Failed to remove member')
    }
  }

  async function handleRevoke(inviteId: number) {
    if (!slug) return
    setRowError(null)
    try {
      await revokeInvite(slug, inviteId)
      setInvites((prev) => (prev ?? []).filter((i) => i.id !== inviteId))
      setReissued((prev) => (prev && invites?.find((i) => i.id === inviteId)?.email === prev.email ? null : prev))
    } catch (e) {
      setRowError(e instanceof Error ? e.message : 'Failed to revoke invite')
    }
  }

  async function copyInviteLink(inv: InviteOut) {
    try {
      await navigator.clipboard.writeText(inviteLink(inv.token))
      setCopiedInviteId(inv.id)
    } catch {
      // clipboard blocked — fall back to putting the link on screen to select by hand
      setReissued({ email: inv.email, link: inviteLink(inv.token), status: null })
    }
  }

  async function handleReissue(inv: InviteOut) {
    if (!slug) return
    setRowError(null)
    setReissuingId(inv.id)
    setCopiedInviteId(null)
    try {
      const fresh = await reissueInvite(slug, inv.id)
      setInvites((prev) => (prev ?? []).map((i) => (i.id === fresh.id ? fresh : i)))
      setReissued({ email: fresh.email, link: inviteLink(fresh.token), status: fresh.email_status ?? null })
    } catch (e) {
      setRowError(e instanceof Error ? e.message : 'Failed to resend the invite')
    } finally {
      setReissuingId(null)
    }
  }

  async function handleCreateInvite(e: FormEvent) {
    e.preventDefault()
    if (!slug || !email.trim()) return
    setCreating(true)
    setCreateError(null)
    setCreated(null)
    setCopied(false)
    try {
      const inv = await createInvite(slug, email.trim(), role)
      // Re-inviting an address with an outstanding invite returns THAT row
      // (re-armed server-side), not a new one — replace, don't duplicate.
      setInvites((prev) => [inv, ...(prev ?? []).filter((i) => i.id !== inv.id)])
      setCreated({ email: inv.email, link: inviteLink(inv.token), status: inv.email_status ?? null })
      setEmail('')
    } catch (e2) {
      setCreateError(e2 instanceof Error ? e2.message : 'Failed to create invite')
    } finally {
      setCreating(false)
    }
  }

  async function handleCopy() {
    if (!created) return
    try {
      await navigator.clipboard.writeText(created.link)
      setCopied(true)
    } catch {
      // clipboard blocked (e.g. insecure context) — the link is still on screen to select by hand
    }
  }

  if (!slug) return null

  return (
    <div className="max-w-4xl">
      {loadError && <div className="mb-4 text-sm text-destructive">{loadError}</div>}
      {rowError && <div className="mb-4 text-sm text-destructive">{rowError}</div>}

      <div className="mb-8">
        <WorkbenchSubHeader title="Members" count={members?.length} />
        {members === null ? (
          <div className="h-24 animate-pulse rounded-lg bg-muted" />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Email</TableHead>
                <TableHead>Role</TableHead>
                {isOwner && <TableHead className="text-right">Actions</TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {members.map((m) => {
                const isSoleOwner = m.role === 'owner' && ownerCount === 1
                return (
                <TableRow key={m.user_id}>
                  <TableCell className="whitespace-normal text-foreground">{m.email}</TableCell>
                  <TableCell className="capitalize">
                    {isOwner ? (
                      <div className="flex flex-col gap-0.5">
                        <select
                          aria-label={`Change role for ${m.email}`}
                          aria-describedby={isSoleOwner ? `sole-owner-hint-${m.user_id}` : undefined}
                          value={m.role}
                          disabled={isSoleOwner || savingRoleFor === m.user_id}
                          onChange={(e) => void handleRoleChange(m.user_id, e.target.value as MemberRole)}
                          className="h-8 rounded-lg border border-input bg-input px-2 text-sm text-foreground capitalize disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {ROLES.map((r) => (
                            <option key={r} value={r} className="capitalize">
                              {r}
                            </option>
                          ))}
                        </select>
                        {/* Visible, not just a `title` tooltip — a disabled
                            <select> isn't focusable, so a hover-only
                            explanation is invisible to screen readers and
                            touch. */}
                        {isSoleOwner && (
                          <span
                            id={`sole-owner-hint-${m.user_id}`}
                            className="text-[11px] normal-case text-muted-foreground"
                          >
                            Only owner — promote someone else first
                          </span>
                        )}
                      </div>
                    ) : (
                      m.role
                    )}
                  </TableCell>
                  {isOwner && (
                    <TableCell className="text-right">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => void handleRemoveMember(m.user_id)}
                        aria-label={`Remove ${m.email}`}
                      >
                        Remove
                      </Button>
                    </TableCell>
                  )}
                </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}
      </div>

      <div className="mb-8">
        <WorkbenchSubHeader title="Pending invites" count={outstandingInvites.length} />
        {reissued && (
          <div className="mb-3 rounded-lg border border-primary/30 bg-primary/5 p-4">
            <p className="text-[12px] font-semibold text-foreground">
              {reissued.status === null
                ? `Invite link for ${reissued.email}:`
                : `New link for ${reissued.email}; the previous one no longer works. ${emailOutcomeText(reissued.status, reissued.email)}`}
            </p>
            <code className="mt-2 block truncate rounded bg-background px-2 py-1 text-[12px] text-foreground-secondary">
              {reissued.link}
            </code>
          </div>
        )}
        {invites === null ? (
          <div className="h-16 animate-pulse rounded-lg bg-muted" />
        ) : outstandingInvites.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">No pending invites.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Email</TableHead>
                <TableHead>Role</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Invited</TableHead>
                {isOwner && <TableHead className="text-right">Actions</TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {outstandingInvites.map((inv) => {
                const live = isInvitePending(inv)
                return (
                  <TableRow key={inv.id}>
                    <TableCell className="whitespace-normal text-foreground">{inv.email}</TableCell>
                    <TableCell className="capitalize">{inv.role}</TableCell>
                    <TableCell className={live ? 'text-muted-foreground' : 'text-warning'}>
                      {inviteExpiryLabel(inv)}
                    </TableCell>
                    <TableCell className="whitespace-normal text-muted-foreground">
                      {inv.created_at ? new Date(inv.created_at).toLocaleDateString() : '—'}
                      {inv.invited_by_email && <span> · {inv.invited_by_email}</span>}
                      {inv.last_emailed_at && (
                        <div className="text-[11px]">emailed {new Date(inv.last_emailed_at).toLocaleString()}</div>
                      )}
                    </TableCell>
                    {isOwner && (
                      <TableCell className="text-right">
                        <div className="flex justify-end gap-1">
                          {live && (
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              onClick={() => void copyInviteLink(inv)}
                              aria-label={`Copy invite link for ${inv.email}`}
                            >
                              {copiedInviteId === inv.id ? 'Copied!' : 'Copy link'}
                            </Button>
                          )}
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            disabled={reissuingId === inv.id}
                            onClick={() => void handleReissue(inv)}
                            aria-label={`Resend invite to ${inv.email}`}
                            title="Emails a new link; the previous link stops working"
                          >
                            {reissuingId === inv.id ? 'Sending…' : 'Resend'}
                          </Button>
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            onClick={() => void handleRevoke(inv.id)}
                            aria-label={`Revoke invite to ${inv.email}`}
                          >
                            Revoke
                          </Button>
                        </div>
                      </TableCell>
                    )}
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}
      </div>

      {isOwner && (
        <div className="rounded-lg border border-border bg-card p-5">
          <h2 className="mb-3 text-sm font-semibold text-foreground">Invite someone</h2>
          <form onSubmit={(e) => void handleCreateInvite(e)} className="flex flex-wrap items-end gap-3">
            <div className="min-w-[14rem] flex-1">
              <label htmlFor="invite-email" className="mb-1 block text-[11px] text-muted-foreground">
                Email
              </label>
              <Input
                id="invite-email"
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="teammate@example.com"
              />
            </div>
            <div>
              <label htmlFor="invite-role" className="mb-1 block text-[11px] text-muted-foreground">
                Role
              </label>
              <select
                id="invite-role"
                value={role}
                onChange={(e) => setRole(e.target.value as InviteRole)}
                className="h-8 rounded-lg border border-input bg-input px-2 text-sm text-foreground"
              >
                {ROLES.map((r) => (
                  <option key={r} value={r} className="capitalize">
                    {r}
                  </option>
                ))}
              </select>
            </div>
            <Button type="submit" disabled={creating || !email.trim()}>
              {creating ? 'Sending…' : 'Send invite'}
            </Button>
          </form>

          {createError && <p className="mt-3 text-sm text-destructive">{createError}</p>}

          {created && (
            <div
              className={
                created.status === 'sent'
                  ? 'mt-4 rounded-lg border border-success/30 bg-success/5 p-4'
                  : 'mt-4 rounded-lg border border-warning/30 bg-warning/5 p-4'
              }
            >
              <p className="text-[12px] font-semibold text-foreground">
                {emailOutcomeText(created.status, created.email)}
              </p>
              <div className="mt-2 flex items-center gap-2">
                <code className="min-w-0 flex-1 truncate rounded bg-background px-2 py-1 text-[12px] text-foreground-secondary">
                  {created.link}
                </code>
                <Button type="button" size="sm" variant="outline" onClick={() => void handleCopy()}>
                  {copied ? 'Copied!' : 'Copy link'}
                </Button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
