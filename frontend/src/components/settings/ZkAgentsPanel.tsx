import { useEffect, useState } from 'react'
import { listAgents } from '@/api/agents'
import { getZkState, type ZkStateOut } from '@/api/vault'

/**
 * "Your agents" on the Security card (canopy-web#1550): where each agent that
 * keeps its secrets in canopy-web stands — status only. Keeping them private
 * is not a step anyone takes per agent: a new agent's secrets are sealed to
 * its owner and approved machines as they arrive, and approving a machine
 * (one passkey touch, under Your machines) gives it access to every agent it
 * runs, creating an agent's key on the way if it has none. So the only thing
 * that can be waiting here is a machine or a person, and the row says where to
 * let them in.
 */

interface AgentRow {
  slug: string
  name: string
  zk: ZkStateOut
}

/** Who is genuinely waiting. An agent with no key of its own yet (kv 0) seals
 *  each secret to everyone allowed as it arrives, so nobody waits on it; a
 *  machine approved later gets it in its approval. */
function waitingOn(zk: ZkStateOut) {
  return zk.enabled && zk.kv > 0 ? zk.pending : []
}

function statusText(r: AgentRow): string {
  if (!r.zk.enabled) return 'Not private yet'
  const pending = waitingOn(r.zk)
  const machines = pending.filter((p) => p.kind === 'device').map((p) => p.runner_name || 'a machine')
  const people = pending.filter((p) => p.kind === 'member').map((p) => p.email || 'someone')
  if (machines.length) return `${machines.join(', ')} waiting — approve it under Your machines`
  if (people.length) return `${people.join(', ')} waiting — let them in on ${r.name}’s page`
  return 'Private to you ✓'
}

async function findAgents(): Promise<AgentRow[]> {
  // Only agents whose secrets live in this vault: a 1Password agent has nothing
  // here, even when you administer it (or administer everything).
  const agents = (await listAgents({ limit: 200 })).items.filter((a) => a.credential_source === 'canopy-web')
  const rows = await Promise.all(agents.map(async (a): Promise<AgentRow | null> => {
    try {
      return { slug: a.slug, name: a.name || a.slug, zk: await getZkState(a.slug) } // refuses unless you administer it
    } catch {
      return null
    }
  }))
  return rows.filter((r): r is AgentRow => r !== null)
}

export function ZkAgentsPanel() {
  const [rows, setRows] = useState<AgentRow[] | null>(null)

  useEffect(() => {
    findAgents().then(setRows).catch(() => setRows([]))
  }, [])

  if (rows === null) return <p className="text-sm text-muted-foreground">Loading…</p>
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground" data-testid="zk-agents-none">No agents keep secrets here yet.</p>
  }
  return (
    <div className="space-y-2" data-testid="settings-zk-agents">
      <p className="text-xs text-muted-foreground">
        Only you and the machines you approve can read these agents&rsquo; secrets — canopy-web can&rsquo;t.
      </p>
      <ul className="divide-y divide-border text-sm">
        {rows.map((r) => {
          const pending = waitingOn(r.zk)
          const ok = r.zk.enabled && pending.length === 0
          return (
            <li key={r.slug} className="flex items-center justify-between gap-2 py-1.5" data-testid={`zk-agent-${r.slug}`}>
              <a href={`/agents/${r.slug}/settings#cred-zk`} className="text-foreground hover:underline">{r.name}</a>
              <span className={`text-xs ${ok ? 'text-success' : 'text-warning'}`} data-testid={`zk-agent-status-${r.slug}`}>
                {pending.some((p) => p.kind === 'device')
                  ? <a href="#your-machines" className="hover:underline">{statusText(r)}</a>
                  : statusText(r)}
              </span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
