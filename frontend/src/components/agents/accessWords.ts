// The words the access list is read in. Kept out of the .tsx files so fast refresh keeps working.
//
// Vocabulary: a role is a named
// bundle of permissions (read, write, agent_edit, use:<site key>) a workspace defines; read_only,
// read_write and editor are the built-ins; admin and owner are fixed, never lines. A line gives ONE role or, rarely, ONE host site. Say "agent admin"
// or "workspace admin", never a bare "admin"; "member", "viewer" and "user" are not agent roles.

import type { AccessLineIn, AccessRoleOut, AgentAccessRowOut } from '@/api/agents'
import { isReadModeName } from '@/components/chat/turnContextWords'

export type LineKind = AccessLineIn['kind']

/** What a person adds, in the order the form offers it. */
export const KIND_OPTIONS: { kind: LineKind; label: string }[] = [
  { kind: 'person', label: 'A person (by email)' },
  { kind: 'workspace', label: 'Everyone in a workspace' },
  { kind: 'domain', label: 'Anyone at an email domain (verified)' },
  { kind: 'contact', label: 'Any known visitor or sender' },
]

/** Lines that name people only by an address a message can claim: they can give read only or open a
 *  host site, never a role that drives the whole agent. */
export const ADDRESS_ONLY_KINDS: readonly LineKind[] = ['domain', 'contact']

/** The roles a line may give, weakest first: what the server sent (`roles`, strongest first), without the
 *  fixed ones (owner and admin come with the agent, never from a line). */
export function assignable(roles: readonly AccessRoleOut[]): AccessRoleOut[] {
  return roles.filter((r) => !r.fixed).reverse()
}

/** Does this role let its holder drive or change the agent? Refused for an address-only line, and a
 *  workspace default of one is the workspace owner's call. */
export function drives(role: string | undefined, roles: readonly AccessRoleOut[]): boolean {
  return roles.find((r) => r.role === role)?.contains_write ?? false
}

/** A workspace role floor for "everyone in a workspace". '' = anyone in it. */
export const AT_LEAST_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'Anyone in it' },
  { value: 'viewer', label: 'At least workspace viewer' },
  { value: 'editor', label: 'At least workspace editor' },
  { value: 'admin', label: 'At least workspace admin' },
  { value: 'owner', label: 'Workspace owners only' },
]

/** "can ask" / "can drive the whole agent, change its skills..." from the server's phrases. */
export function canText(role: string, roles: readonly AccessRoleOut[]): string {
  const can = roles.find((r) => r.role === role)?.can ?? []
  return can.length ? `can ${can.join(', ')}` : ''
}

/** A role as it reads in a sentence: the agent's roles are never bare "admin". */
export function roleWord(role: string | null | undefined, roles?: readonly AccessRoleOut[]): string {
  if (!role) return 'No access'
  const named = roles?.find((r) => r.role === role)?.label
  if (named && role !== 'admin') return named
  if (role === 'admin') return 'Agent admin'
  if (role === 'system') return 'The agent itself'
  if (role === 'read_only') return 'Read only'
  if (role === 'read_write') return 'Read & write'
  return role.charAt(0).toUpperCase() + role.slice(1)
}

/** What a line gives, in words: its role, or the one host site it opens. */
export function lineWhat(line: { role: string; host_site?: string }, roles?: readonly AccessRoleOut[]): string {
  return line.role ? roleWord(line.role, roles) : `Only the “${line.host_site ?? ''}” host site`
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
  if (s.startsWith('capability:') && isReadModeName(s.slice(11))) return 'In read mode'
  if (s.startsWith('capability:')) return `From the "${s.slice(11)}" host site`
  return r.role ? 'Set on this agent' : 'No line names them'
}
