// Shared, non-component pieces of the access-request pages (kept out of the
// .tsx files so React fast refresh keeps working on them).
import type { AccessRequestOut, ApprovableRole } from '@/api/workspaces'
import { grantableRoles } from '@/lib/workspaceRoles'

/** Roles an approval can grant: never owner (ownership is handed over on the
 *  Members list), and only what `myRole` may grant (an admin: viewer, editor). */
export function approvableRoles(myRole: string | null | undefined): ApprovableRole[] {
  return grantableRoles(myRole).filter((r): r is ApprovableRole => r !== 'owner')
}

export function requesterLabel(req: Pick<AccessRequestOut, 'name' | 'email'>): string {
  return req.name ? `${req.name} (${req.email})` : req.email
}

export function emailDomain(email: string): string {
  const at = email.lastIndexOf('@')
  return at >= 0 ? email.slice(at + 1) : ''
}

/** One line saying what was decided, for a decided request. */
export function decisionText(req: AccessRequestOut): string {
  if (req.status === 'pending') return 'Waiting for an admin or owner.'
  const when = req.decided_at ? ` on ${new Date(req.decided_at).toLocaleString()}` : ''
  if (req.status === 'approved') {
    const by = req.auto ? 'automatically (this workspace auto-approves requests)' : `by ${req.decided_by_email ?? 'an admin'}`
    return `Approved as ${req.role} ${by}${when}.`
  }
  return `Denied by ${req.decided_by_email ?? 'an admin'}${when}.`
}

/** What happened when the admins were told, or null when nothing worth saying. */
export function notifyProblem(req: AccessRequestOut): string | null {
  const n = req.notify_result as { failed?: string[]; not_configured?: boolean; recipients?: number } | null | undefined
  if (!n) return null
  if (n.failed && n.failed.length > 0) return `Could not email: ${n.failed.join(', ')}`
  if (n.not_configured) return 'Email delivery is off — nobody was emailed about this request.'
  if (n.recipients === 0) return 'This workspace has no admins or owners to tell.'
  return null
}
