import { useContext, useEffect, useState } from 'react'
import { listAgents } from '@/api/agents'
import { listRunnerDeviceKeys, type RunnerDeviceKeyOut } from '@/api/deviceKeys'
import { getZkState, type ZkStateOut } from '@/api/vault'
import { AuthContext } from '@/auth/AuthProvider'
import { scopedPath } from '@/lib/scopedLinks'
import { machineShortName } from './machineText'

/**
 * "Your agents" on the Security card (canopy-web#1550): where each agent that
 * keeps its secrets in canopy-web stands — status only. Keeping them private
 * is not a step anyone takes per agent: a new agent's secrets are sealed to
 * its owner and approved machines as they arrive, and approving a machine
 * (one passkey touch, under Your runners) gives it access to every agent it
 * runs, creating an agent's key on the way if it has none. So the only thing
 * that can be waiting here is a machine or a person, and the row says where to
 * let them in.
 */

interface AgentRow {
  slug: string
  workspace: string | null | undefined
  name: string
  zk: ZkStateOut
}

/** Who is genuinely waiting. An agent with no key of its own yet (kv 0) seals
 *  each secret to everyone allowed as it arrives, so nobody waits on it; a
 *  machine approved later gets it in its approval. */
function waitingOn(zk: ZkStateOut) {
  return zk.kv > 0 ? zk.pending : []
}

function statusText(r: AgentRow): string {
  const pending = waitingOn(r.zk)
  const machines = pending.filter((p) => p.kind === 'device').map((p) => p.runner_name || 'a machine')
  const people = pending.filter((p) => p.kind === 'member').map((p) => p.email || 'someone')
  if (machines.length) return `${machines.join(', ')} waiting — approve it under Your runners`
  if (people.length) return `${people.join(', ')} waiting — let them in on ${r.name}’s page`
  return 'Private ✓'
}

/** Where to let a waiting machine in: its own row under Your runners, marked
 *  (`?device_key=`), which offers "Give access" — one passkey touch for every
 *  agent that machine waits on. */
function machinesHref(pending: ZkStateOut['pending']): string {
  const ids = [...new Set(pending.filter((p) => p.kind === 'device').map((p) => p.target))]
  return ids.length === 1 ? `/settings?device_key=${ids[0]}#your-runners` : '#your-runners'
}

/** Who holds a copy of the agent's key right now, in plain words: "you",
 *  a colleague's email, "MacBook (acedimagi)", "cloud-ec2-2". An agent with no
 *  key of its own yet (kv 0) seals each secret to its owner and approved
 *  machines as it arrives, so it is said that way. */
function openedBy(zk: ZkStateOut, me: string, keys: RunnerDeviceKeyOut[]): string {
  if (zk.kv === 0) return 'you and the runners you approve'
  const byId = new Map(keys.map((k) => [k.id, k]))
  const current = (zk.wraps ?? []).filter((w) => w.kv === zk.kv && w.current !== false)
  const people = current.filter((w) => w.kind === 'member')
    .map((w) => (me && w.label === me ? 'you' : w.label || 'someone'))
    .sort((a, b) => (a === 'you' ? -1 : b === 'you' ? 1 : a.localeCompare(b)))
  const machines = current.filter((w) => w.kind === 'device').map((w) => {
    const k = w.target ? byId.get(w.target) : undefined
    return k ? machineShortName(k) : (w.label || 'a machine').split(' · ')[0]
  })
  const all = [...people, ...machines]
  return all.length ? all.join(', ') : 'nobody yet'
}

async function findAgents(): Promise<AgentRow[]> {
  const agents = (await listAgents({ limit: 200 })).items
  const rows = await Promise.all(agents.map(async (a): Promise<AgentRow | null> => {
    try {
      return { slug: a.slug, workspace: a.workspace, name: a.name || a.slug, zk: await getZkState(a.slug) } // refuses unless you administer it
    } catch {
      return null
    }
  }))
  return rows.filter((r): r is AgentRow => r !== null)
}

export function ZkAgentsPanel() {
  const [rows, setRows] = useState<AgentRow[] | null>(null)
  const [keys, setKeys] = useState<RunnerDeviceKeyOut[]>([])
  const me = useContext(AuthContext).user?.email ?? ''

  useEffect(() => {
    findAgents().then(setRows).catch(() => setRows([]))
    // Only to name machines plainly; without it a wrap's own label is used.
    listRunnerDeviceKeys().then(setKeys).catch(() => setKeys([]))
  }, [])

  if (rows === null) return <p className="text-sm text-muted-foreground">Loading…</p>
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground" data-testid="zk-agents-none">No agents keep secrets here yet.</p>
  }
  return (
    <div className="space-y-2" data-testid="settings-zk-agents">
      <p className="text-xs text-muted-foreground">
        Who holds a copy of each agent&rsquo;s key — canopy-web never does.
      </p>
      <ul className="divide-y divide-border text-sm">
        {rows.map((r) => {
          const pending = waitingOn(r.zk)
          const ok = pending.length === 0
          return (
            <li key={r.slug} className="flex flex-col gap-1 py-1.5 sm:flex-row sm:items-center sm:justify-between sm:gap-3"
                data-testid={`zk-agent-${r.slug}`}>
              <p className="min-w-0">
                <a href={r.workspace ? scopedPath(r.workspace, `/agents/${r.slug}/settings#cred-zk`) : `/agents/${r.slug}/settings#cred-zk`} className="font-medium text-foreground hover:underline">{r.name}</a>
                <span className="text-muted-foreground" data-testid={`zk-agent-holders-${r.slug}`}>
                  {' — can be opened by: '}{openedBy(r.zk, me, keys)}
                </span>
              </p>
              <span className={`shrink-0 text-xs ${ok ? 'text-success' : 'text-warning'}`} data-testid={`zk-agent-status-${r.slug}`}>
                {pending.some((p) => p.kind === 'device')
                  ? <a href={machinesHref(pending)} className="underline">{statusText(r)}</a>
                  : statusText(r)}
              </span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
