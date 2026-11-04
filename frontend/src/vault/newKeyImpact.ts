/**
 * What creating a new vault key costs this person, for the screen that asks them.
 *
 * A new key stops every copy of a vault's key they hold from opening (`zk.put_keyset`
 * drops the member wraps); the vault and its values stay. So the question to answer is
 * which vaults they hold a copy of, and who can give them a fresh one: a person who
 * manages the vault and holds a copy (granting is admin-gated), or a keeper machine,
 * which passes copies on by itself.
 *
 * A read that fails lands in `unread` and never reads as "nothing at stake": a quiet
 * empty list here would tell someone they risk nothing when we simply did not look.
 */
import {
  getVaultMembers,
  listMyAgentsWithVaults,
  listMyWorkspacesWithVaults,
  listWorkspaceVaults,
  VaultApiError,
  type VaultMemberOut,
} from '@/api/vault'

export interface VaultImpact {
  id: string
  name: string
  kind: 'agent' | 'workspace'
  /** Owners and admins holding a copy, and keeper machines. */
  letsYouBackIn: string[]
  /** Nobody else can let them back in: check with an owner or admin first. */
  alone: boolean
}

export interface NewKeyImpact {
  vaults: VaultImpact[]
  /** What could not be read, in words; they may hold a copy of these too. */
  unread: string[]
}

const AGENT_LIMIT = 500
const MANAGES = new Set(['owner', 'admin'])

function impactOf(
  v: { id: string; name: string; kind: VaultImpact['kind']; members: readonly VaultMemberOut[]; keepers: readonly string[] },
  userId: string,
): VaultImpact | null {
  const isMe = (m: VaultMemberOut) => String(m.user_id) === userId
  if (!v.members.some((m) => isMe(m) && m.has_copy)) return null
  const letsYouBackIn = [
    ...v.members.filter((m) => !isMe(m) && m.has_copy && MANAGES.has(m.role)).map((m) => m.email),
    ...v.keepers.map((k) => `${k} (a machine)`),
  ]
  return { id: v.id, name: v.name, kind: v.kind, letsYouBackIn, alone: letsYouBackIn.length === 0 }
}

async function agentVaults(userId: string, unread: string[]): Promise<VaultImpact[]> {
  let agents
  try {
    agents = await listMyAgentsWithVaults(AGENT_LIMIT)
  } catch {
    unread.push('your agents’ vaults')
    return []
  }
  if (agents.total > agents.items.length) unread.push(`${agents.total - agents.items.length} more agents’ vaults`)
  const reads = await Promise.all(agents.items.map(async (a) => {
    try {
      const v = await getVaultMembers(a.slug)
      return impactOf({ id: v.vault_id, name: a.name || a.slug, kind: 'agent', members: v.members,
                        keepers: v.holders?.keepers ?? [] }, userId)
    } catch (e) {
      // 404: this agent is in no workspace, so it has no vault to hold a copy of.
      if (!(e instanceof VaultApiError && e.status === 404)) unread.push(`${a.name || a.slug}’s vault`)
      return null
    }
  }))
  return reads.filter((r): r is VaultImpact => r !== null)
}

async function workspaceVaults(userId: string, unread: string[]): Promise<VaultImpact[]> {
  let workspaces
  try {
    workspaces = await listMyWorkspacesWithVaults()
  } catch {
    unread.push('your workspaces’ vaults')
    return []
  }
  const reads = await Promise.all(workspaces.map(async (w) => {
    try {
      return (await listWorkspaceVaults(w.slug)).map((v) => impactOf(
        { id: v.vault_id, name: v.name, kind: 'workspace', members: v.members, keepers: v.holders?.keepers ?? [] }, userId))
    } catch {
      unread.push(`the vaults of ${w.name || w.slug}`)
      return []
    }
  }))
  return reads.flat().filter((r): r is VaultImpact => r !== null)
}

/** Every vault this person holds a copy of, the ones nobody else can reopen first. */
export async function readNewKeyImpact(userId: string): Promise<NewKeyImpact> {
  const unread: string[] = []
  const [agents, workspaces] = await Promise.all([agentVaults(userId, unread), workspaceVaults(userId, unread)])
  const byId = new Map([...agents, ...workspaces].map((v) => [v.id, v]))
  const vaults = [...byId.values()].sort((a, b) => Number(b.alone) - Number(a.alone) || a.name.localeCompare(b.name))
  return { vaults, unread }
}
