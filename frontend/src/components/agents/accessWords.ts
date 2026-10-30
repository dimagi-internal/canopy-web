// The words the access list is read in. Kept out of the .tsx files so fast refresh keeps working.
//
// Vocabulary (docs/superpowers/specs/2026-10-30-agent-roles-and-access-list-design.md): an
// agent's roles are viewer < user < editor < admin < owner. Say "agent admin" or "workspace
// admin", never a bare "admin"; "member" is never an agent role.

import type { AccessLineIn, AccessRoleOut, AgentAccessRowOut } from '@/api/agents'

export type LineKind = AccessLineIn['kind']
export type AddableRole = AccessLineIn['role']

/** What a person adds, in the order the form offers it. */
export const KIND_OPTIONS: { kind: LineKind; label: string }[] = [
  { kind: 'person', label: 'A person (by email)' },
  { kind: 'workspace', label: 'Everyone in a workspace' },
  { kind: 'domain', label: 'Anyone at an email domain (verified)' },
  { kind: 'contact', label: 'Any known visitor or sender' },
]

/** Lines that name people only by an address a message can claim: they can give viewer or user, not editor. */
export const ADDRESS_ONLY_KINDS: readonly LineKind[] = ['domain', 'contact']

export const ADDABLE_ROLES: AddableRole[] = ['viewer', 'user', 'editor']

/** A workspace role floor for "everyone in a workspace". '' = anyone in it. */
export const AT_LEAST_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'Anyone in it' },
  { value: 'viewer', label: 'At least workspace viewer' },
  { value: 'editor', label: 'At least workspace editor' },
  { value: 'admin', label: 'At least workspace admin' },
  { value: 'owner', label: 'Workspace owners only' },
]

/** The roles of an agent, lowest first (the server sends them highest first). */
export function ascending(roles: readonly AccessRoleOut[]): AccessRoleOut[] {
  return [...roles].reverse()
}

/** "viewer: ask" / "editor: ask, run the whole agent" from the server's phrases. */
export function canText(role: string, roles: readonly AccessRoleOut[]): string {
  const can = roles.find((r) => r.role === role)?.can ?? []
  return can.length ? `can ${can.join(', ')}` : ''
}

/** A role as it reads in a sentence: the agent's roles are never bare "admin". */
export function roleWord(role: string | null | undefined): string {
  if (!role) return 'No access'
  if (role === 'admin') return 'Agent admin'
  if (role === 'system') return 'The agent itself'
  return role.charAt(0).toUpperCase() + role.slice(1)
}

/** Why a person has the role they have: the server's own words (`via`) when it has them,
 *  else from the roster row's `source`. */
export function sourceWords(r: Pick<AgentAccessRowOut, 'source' | 'role' | 'via'>): string {
  if (r.via && r.source.startsWith('list:')) return r.via
  const s = r.source
  if (s === 'owner') return 'Owns this agent'
  if (s === 'admin') return 'Is an agent admin'
  if (s === 'system') return 'The system'
  if (s.startsWith('list:')) return 'A line on the access list'
  if (s === 'workspace-editor') return 'A workspace editor: every turn needs approval'
  if (s.startsWith('rule:')) return `A published rule (${s.slice(5)})`
  if (s.startsWith('capability:')) return `Through the "${s.slice(11)}" capability`
  return r.role ? 'Set on this agent' : 'No line names them'
}
