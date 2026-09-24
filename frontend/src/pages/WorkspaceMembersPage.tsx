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

const ROLES: InviteRole[] = ['owner', 'editor', 'viewer']

// A dead invite (accepted/revoked/expired) still lives in the API's list —
// this page only surfaces the ones a human might still act on. Pure + exported
// so it's testable without a renderer, mirroring RunnerAssignments' pattern.
export function isInvitePending(inv: InviteOut, now: number = Date.now()): boolean {
  if (inv.accepted_at || inv.revoked_at) return false
  return new Date(inv.expires_at).getTime() > now
}

// Nobody has accepted it and nobody revoked it — pending OR expired. An
// expired invite stays on the page because it is exactly the one an owner
// wants to find: somebody never got round to it, and "New link" revives it.
export function isInviteOutstanding(inv: InviteOut): boolean {
  return !inv.accepted_at && !inv.revoked_at
}

const DAY_MS = 86_400_000

// "Expires in 3 days" / "Expired 2 days ago" — the one fact that decides
// whether the link you are about to copy still works.
export function inviteExpiryLabel(inv: InviteOut, now: number = Date.now()): string {
  const delta = new Date(inv.expires_at).getTime() - now
  const days = Math.round(Math.abs(delta) / DAY_MS)
  if (delta > 0) {
    if (days === 0) return 'Expires today'
    return `Expires in ${days} day${days === 1 ? '' : 's'}`
  }
  if (days === 0) return 'Expired today'
  return `Expired ${days} day${days === 1 ? '' : 's'} ago`
}

// The absolute, copy-pasteable accept link. There is no email delivery (see
// the plan's "Delivery is a copy-link, not an email") — the inviter sends this
// themselves, so it must be unmissable, not a footnote.
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
  const [newInviteLink, setNewInviteLink] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  // Per-row link actions on an outstanding invite: which row's link was just
  // copied, which is mid-reissue, and the fresh link a reissue produced (shown
  // on screen too, since the clipboard can be blocked).
  const [copiedInviteId, setCopiedInviteId] = useState<number | null>(null)
  const [reissuingId, setReissuingId] = useState<number | null>(null)
  const [reissued, setReissued] = useState<{ email: string; link: string } | null>(null)

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
      setReissued({ email: inv.email, link: inviteLink(inv.token) })
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
      setReissued({ email: fresh.email, link: inviteLink(fresh.token) })
      try {
        await navigator.clipboard.writeText(inviteLink(fresh.token))
        setCopiedInviteId(fresh.id)
      } catch {
        // the notice below still shows the link
      }
    } catch (e) {
      setRowError(e instanceof Error ? e.message : 'Failed to send a new link')
    } finally {
      setReissuingId(null)
    }
  }

  async function handleCreateInvite(e: FormEvent) {
    e.preventDefault()
    if (!slug || !email.trim()) return
    setCreating(true)
    setCreateError(null)
    setNewInviteLink(null)
    setCopied(false)
    try {
      const inv = await createInvite(slug, email.trim(), role)
      // Re-inviting an address with an outstanding invite returns THAT row
      // (re-armed server-side), not a new one — replace, don't duplicate.
      setInvites((prev) => [inv, ...(prev ?? []).filter((i) => i.id !== inv.id)])
      setNewInviteLink(inviteLink(inv.token))
      setEmail('')
    } catch (e2) {
      setCreateError(e2 instanceof Error ? e2.message : 'Failed to create invite')
    } finally {
      setCreating(false)
    }
  }

  async function handleCopy() {
    if (!newInviteLink) return
    try {
      await navigator.clipboard.writeText(newInviteLink)
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
              New link for {reissued.email} — the previous link no longer works. Send this one to them yourself.
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
                            aria-label={`Send a new link to ${inv.email}`}
                          >
                            {reissuingId === inv.id ? 'Working…' : 'New link'}
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
              {creating ? 'Sending…' : 'Create invite'}
            </Button>
          </form>

          {createError && <p className="mt-3 text-sm text-destructive">{createError}</p>}

          {newInviteLink && (
            <div className="mt-4 rounded-lg border border-primary/30 bg-primary/5 p-4">
              <p className="text-[12px] font-semibold text-foreground">
                Canopy does not send this invite by email — send this link to them yourself
                (Slack, email, whatever you already use).
              </p>
              <div className="mt-2 flex items-center gap-2">
                <code className="min-w-0 flex-1 truncate rounded bg-background px-2 py-1 text-[12px] text-foreground-secondary">
                  {newInviteLink}
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
