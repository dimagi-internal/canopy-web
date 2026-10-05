/**
 * What a workspace role may do — the frontend mirror of
 * `apps/workspaces/permissions.py`, so a page shows a control only to someone
 * the server will let use it.
 *
 * The SERVER decides; this only avoids offering a button that 403s. Pages ask
 * by capability (`roleAllows(myRole, 'members.manage')`), never by comparing a
 * role string, for the same reason the backend does: adding `admin` between
 * editor and owner silently changed what every `role === 'owner'` meant.
 * `workspaceRoles.test.ts` pins this table to the backend's.
 */

export type WorkspaceRole = 'viewer' | 'editor' | 'admin' | 'owner'

export const ROLE_RANK: Record<WorkspaceRole, number> = { viewer: 0, editor: 1, admin: 2, owner: 3 }

/** Lowest-first, for role pickers. */
export const ROLES_ASCENDING: WorkspaceRole[] = ['viewer', 'editor', 'admin', 'owner']

export type Capability =
  | 'read'
  | 'content.write'
  | 'agent.work'
  | 'session.drive'
  | 'events.write'
  | 'logs.read'
  | 'members.manage'
  | 'integrations'
  | 'runners.route'
  | 'retention.manage'
  | 'own'

export const MINIMUM_ROLE: Record<Capability, WorkspaceRole> = {
  read: 'viewer',
  'content.write': 'editor',
  'agent.work': 'editor',
  'session.drive': 'editor',
  'events.write': 'editor',
  'logs.read': 'admin',
  'members.manage': 'admin',
  integrations: 'admin',
  'runners.route': 'admin',
  'retention.manage': 'admin',
  own: 'owner',
}

function rank(role: string | null | undefined): number {
  return role && role in ROLE_RANK ? ROLE_RANK[role as WorkspaceRole] : -1
}

export function roleAllows(role: string | null | undefined, capability: Capability): boolean {
  return rank(role) >= ROLE_RANK[MINIMUM_ROLE[capability]]
}

/**
 * May `actor` change a member holding `target` (to `next`), or invite at
 * `next`? An owner may do anything; anyone else with members.manage acts
 * strictly below themselves and grants only roles below themselves.
 */
export function mayManageMember(
  actor: string | null | undefined,
  target: string | null,
  next: string | null = null,
): boolean {
  if (actor === 'owner') return true
  if (!roleAllows(actor, 'members.manage')) return false
  const mine = rank(actor)
  if (target !== null && rank(target) >= mine) return false
  if (next !== null && rank(next) >= mine) return false
  return true
}

/** The roles `actor` may hand out, lowest first. */
export function grantableRoles(actor: string | null | undefined): WorkspaceRole[] {
  return ROLES_ASCENDING.filter((r) => mayManageMember(actor, null, r))
}
