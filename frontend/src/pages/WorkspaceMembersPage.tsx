import { useEffect, useMemo, useState, type JSX } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Button, Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from 'canopy-ui/ui'
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
import { AddPersonForm, PeopleTable, type PersonRow } from '@/components/people/PeopleTable'
import { roleOptions } from '@/components/people/roles'
import { grantableRoles, mayManageMember, roleAllows } from '@/lib/workspaceRoles'
import { emailOutcomeText, inviteExpiryLabel, isInviteOutstanding, isInvitePending, type EmailStatus } from './workspaceInvites'


// The absolute, copy-pasteable accept link — the same one the email carries,
// and the fallback whenever the email did not go out.
function inviteLink(token: string): string {
  const base = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')
  return `${window.location.origin}${base}/invite/${token}`
}

// What each workspace role means, said plainly (docs/architecture/access.md).
// Workspace roles are not agent roles: a workspace ADMIN runs the workspace but
// holds no agent's keys; a workspace OWNER is every agent's admin.
const ROLE_HELP: { role: string; text: string }[] = [
  { role: 'Viewer', text: 'Reads the workspace and talks to agents through what each agent offers members.' },
  {
    role: 'Editor',
    text: 'Creates and changes content, edits agents and sends them work — those turns always run manual, so anything outbound waits for an agent admin.',
  },
  {
    role: 'Admin',
    text: 'Runs the workspace: every log, members below admin, the integrations. Holds no agent keys — not an admin of any agent unless made one.',
  },
  {
    role: 'Owner',
    text: "Holds the keys: the shared vault, the Slack app, connected sites, deleting the workspace — and is every agent's admin.",
  },
]

export function WorkspaceMembersPage(): JSX.Element | null {
  const { workspace: slug } = useParams()
  const navigate = useNavigate()
  const { workspaces, refresh: refreshWorkspaces } = useWorkspace()
  // What I may do here, asked by capability (lib/workspaceRoles mirrors
  // apps/workspaces/permissions.py): an admin manages members and invites
  // strictly below admin; an owner manages everyone.
  const myRole = workspaces.find((w) => w.slug === slug)?.role
  const canManage = roleAllows(myRole, 'members.manage')
  const grantable = grantableRoles(myRole) as InviteRole[]
  const auth = useAuth()
  const myEmail = auth.status === 'authenticated' ? auth.user.email.toLowerCase() : null

  const [members, setMembers] = useState<MemberOut[] | null>(null)
  const [invites, setInvites] = useState<InviteOut[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [rowError, setRowError] = useState<string | null>(null)

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
  // Owners of a parent workspace own this one too (`inherited`). They count
  // toward "is anyone else an owner" — the server lets the last DIRECT owner
  // step down beside them — but they have no row here to change or remove.
  const directOwnerCount = useMemo(
    () => (members ?? []).filter((m) => m.role === 'owner' && !m.inherited).length,
    [members],
  )
  const hasInheritedOwner = useMemo(() => (members ?? []).some((m) => m.inherited), [members])

  // Both reject on failure: the shared PeopleTable shows the error inline on
  // the row that failed, which is where the person is looking.
  async function handleRoleChange(userId: number, role: MemberRole) {
    if (!slug) return
    const updated = await setMemberRole(slug, userId, role)
    setMembers((prev) => (prev ?? []).map((m) => (m.user_id === userId ? updated : m)))
    // The cached workspace list (header switcher, this page's own `canManage`)
    // is fetched once and otherwise never invalidated — if I just changed
    // MY OWN role, refresh it now so `canManage` reflects reality immediately
    // instead of continuing to render owner-only controls I can no longer
    // use until a full page reload (see WorkspaceProvider.refresh's docstring).
    if (myEmail && updated.email.toLowerCase() === myEmail) {
      void refreshWorkspaces()
    }
  }

  async function handleRemoveMember(userId: number) {
    if (!slug) return
    await removeMember(slug, userId)
    setMembers((prev) => (prev ?? []).filter((m) => m.user_id !== userId))
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

  // Rejects on failure; the shared add row shows the server's words inline.
  async function handleCreateInvite(email: string, role: InviteRole) {
    if (!slug) return
    setCreated(null)
    setCopied(false)
    const inv = await createInvite(slug, email, role)
    // Re-inviting an address with an outstanding invite returns THAT row
    // (re-armed server-side), not a new one — replace, don't duplicate.
    setInvites((prev) => [inv, ...(prev ?? []).filter((i) => i.id !== inv.id)])
    setCreated({ email: inv.email, link: inviteLink(inv.token), status: inv.email_status ?? null })
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
          <PeopleTable
            actions={canManage}
            rows={members.map((m): PersonRow => {
              const isSoleOwner =
                m.role === 'owner' && !m.inherited && directOwnerCount === 1 && !hasInheritedOwner
              // A row I may act on: below me (an owner may act on anyone).
              const manageable = !m.inherited && mayManageMember(myRole, m.role)
              return {
                key: m.user_id,
                name: m.email,
                role: m.role,
                options: roleOptions(grantable),
                editable: manageable,
                roleDisabled: isSoleOwner,
                why: m.inherited
                  ? 'via parent workspace'
                  : manageable && isSoleOwner
                    ? 'Only owner — promote someone else first'
                    : null,
                onRoleChange: (next) => handleRoleChange(m.user_id, next as MemberRole),
                onRemove: manageable ? () => handleRemoveMember(m.user_id) : undefined,
              }
            })}
          />
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
                {canManage && <TableHead className="text-right">Actions</TableHead>}
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
                    {canManage && (
                      <TableCell className="text-right">
                        {mayManageMember(myRole, null, inv.role) && (
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
                        )}
                      </TableCell>
                    )}
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}
      </div>

      <div className="rounded-lg border border-border bg-card p-5" data-testid="workspace-role-help">
        <h2 className="mb-2 text-sm font-semibold text-foreground">What each role can do</h2>
        <dl className="m-0 grid gap-1 text-[12px] text-foreground-secondary sm:grid-cols-[6rem_1fr]">
          {ROLE_HELP.map((r) => (
            <div key={r.role} className="contents">
              <dt className="font-medium text-foreground">{r.role}</dt>
              <dd className="m-0">{r.text}</dd>
            </div>
          ))}
        </dl>
      </div>

      {canManage && (
        <div className="rounded-lg border border-border bg-card p-5">
          <h2 className="mb-3 text-sm font-semibold text-foreground">Invite someone</h2>
          <AddPersonForm
            options={roleOptions(grantable)}
            defaultRole="editor"
            onAdd={(email, role) => handleCreateInvite(email, role as InviteRole)}
            submitLabel="Send invite"
            busyLabel="Sending…"
            placeholder="teammate@example.com"
          />

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
