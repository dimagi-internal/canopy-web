import { useEffect, useMemo, useState, type FormEvent, type JSX } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Button, Input, Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from 'canopy-ui/ui'
import { WorkbenchSubHeader } from 'canopy-ui'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import {
  createInvite,
  listInvites,
  listMembers,
  removeMember,
  revokeInvite,
  WorkspaceApiError,
  type InviteOut,
  type InviteRole,
  type MemberOut,
} from '@/api/workspaces'

const ROLES: InviteRole[] = ['owner', 'editor', 'viewer']

// A dead invite (accepted/revoked/expired) still lives in the API's list —
// this page only surfaces the ones a human might still act on. Pure + exported
// so it's testable without a renderer, mirroring RunnerAssignments' pattern.
export function isInvitePending(inv: InviteOut, now: number = Date.now()): boolean {
  if (inv.accepted_at || inv.revoked_at) return false
  return new Date(inv.expires_at).getTime() > now
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
  const { workspaces } = useWorkspace()
  const isOwner = workspaces.find((w) => w.slug === slug)?.role === 'owner'

  const [members, setMembers] = useState<MemberOut[] | null>(null)
  const [invites, setInvites] = useState<InviteOut[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [rowError, setRowError] = useState<string | null>(null)

  const [email, setEmail] = useState('')
  const [role, setRole] = useState<InviteRole>('editor')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [newInviteLink, setNewInviteLink] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

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

  const pendingInvites = useMemo(() => (invites ?? []).filter((i) => isInvitePending(i)), [invites])

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
    } catch (e) {
      setRowError(e instanceof Error ? e.message : 'Failed to revoke invite')
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
      setInvites((prev) => [inv, ...(prev ?? [])])
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
    <div className="max-w-4xl px-6 py-8">
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
              {members.map((m) => (
                <TableRow key={m.user_id}>
                  <TableCell className="whitespace-normal text-foreground">{m.email}</TableCell>
                  <TableCell className="capitalize">{m.role}</TableCell>
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
              ))}
            </TableBody>
          </Table>
        )}
      </div>

      <div className="mb-8">
        <WorkbenchSubHeader title="Pending invites" count={pendingInvites.length} />
        {invites === null ? (
          <div className="h-16 animate-pulse rounded-lg bg-muted" />
        ) : pendingInvites.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">No pending invites.</p>
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
              {pendingInvites.map((inv) => (
                <TableRow key={inv.id}>
                  <TableCell className="whitespace-normal text-foreground">{inv.email}</TableCell>
                  <TableCell className="capitalize">{inv.role}</TableCell>
                  {isOwner && (
                    <TableCell className="text-right">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        onClick={() => void handleRevoke(inv.id)}
                        aria-label={`Revoke invite to ${inv.email}`}
                      >
                        Revoke
                      </Button>
                    </TableCell>
                  )}
                </TableRow>
              ))}
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
