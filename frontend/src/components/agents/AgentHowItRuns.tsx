import { useEffect, useState, type JSX, type ReactNode } from 'react'
import { Link } from 'react-router-dom'

import {
  getAgentDefaultOrder,
  getAgentRunnerRules,
  listAgentLicenses,
  type AgentLicenseOut,
} from '@/api/agents'
import { getAgentDeviceKeys, listRunnerDeviceKeys, type RunnerDeviceKeyOut } from '@/api/deviceKeys'
import { listRunners, type RunnerOut } from '@/api/harness'
import { listSchedules } from '@/api/schedules'
import { getZkState, type ZkStateOut } from '@/api/vault'
import { useAuth } from '@/auth/AuthProvider'
import { relativeAge } from '@/lib/relativeAge'
import { CopyCommand } from './CopyCommand'

// THE TWO WAYS AN AGENT RUNS, side by side (docs/architecture/runners.md →
// "Two ways an agent runs"):
//
//   1. Use it yourself in Claude Code — no runner. `canopy agent use <slug>` once
//      on your computer; your sessions are recorded as local turns.
//   2. Let it run on its own — a runner (your own Mac set Private, or a fleet
//      cloud box) takes the turns schedules, mail and dispatches start.
//
// A summary, not a second control: each card links to the detailed setting it
// summarises (People → Local use, Routing, Credentials, Schedules). Every read is
// best effort — one that fails or is refused (the vault reads are admins-only)
// leaves its part out rather than breaking the card.

export type Light = 'ready' | 'waiting' | 'off'

const LIGHT_DOT: Record<Light, string> = {
  ready: 'bg-success',
  waiting: 'bg-warning',
  off: 'bg-muted-foreground',
}

function Dot({ light, label }: { light: Light; label: string }): JSX.Element {
  return <span role="img" aria-label={label} className={`inline-block h-2 w-2 shrink-0 rounded-full ${LIGHT_DOT[light]}`} />
}

/** Whether the person looking can use the agent in their own Claude Code now:
 *  one of THEIR computers has an approved key, and — for a zero-knowledge vault —
 *  a grant to open it. A computer is either a private runner app they own (in the
 *  runner list) or a computer with no runner app, whose key holder is a `computer`
 *  runner that no runner list shows: its keys are in theirs (only its owner holds
 *  any). Pure, so it is tested alone. */
export function viewerReadiness({
  slug,
  email,
  license,
  runners,
  keys,
}: {
  slug: string
  email: string
  license: AgentLicenseOut | undefined
  runners: readonly RunnerOut[]
  keys: readonly RunnerDeviceKeyOut[]
}): { light: Light; text: string } {
  if (license?.status === 'revoked') {
    return { light: 'off', text: 'Your licence was revoked by its admins.' }
  }
  const mine = new Map(
    runners.filter((r) => r.private && r.owner_email === email).map((r) => [String(r.id), r.name]),
  )
  for (const k of keys) if (k.runner_kind === 'computer') mine.set(String(k.runner), k.runner_name)
  const myKeys = keys.filter((k) => mine.has(String(k.runner)) && k.status !== 'revoked')
  const name = (k: RunnerDeviceKeyOut) => mine.get(String(k.runner)) ?? k.runner_name
  const opens = myKeys.find((k) => k.status === 'active' && (k.can_open ?? []).some((a) => a.slug === slug))
  if (opens) return { light: 'ready', text: `Ready: ${name(opens)} can read its secrets.` }
  const pendingKey = myKeys.find((k) => k.status === 'pending')
  if (pendingKey) {
    return { light: 'waiting', text: `Waiting: ${name(pendingKey)}'s key needs approving (Settings → Runners).` }
  }
  const grant = myKeys.find((k) => (k.zk_pending_agents ?? []).includes(slug))
  if (grant) return { light: 'waiting', text: `Grant pending: ${name(grant)} has no access to its vault yet.` }
  const active = myKeys.find((k) => k.status === 'active')
  if (active && license?.status === 'active') {
    return { light: 'ready', text: `Ready: ${name(active)} has an approved key and you hold a licence.` }
  }
  if (active) {
    return { light: 'waiting', text: `${name(active)} has an approved key; your first session asks for a licence.` }
  }
  return { light: 'off', text: 'Not set up on your computers yet. Run the command above.' }
}

export type VaultReach = 'reads' | 'grant-pending' | 'key-pending' | 'none'

const VAULT_LABEL: Record<VaultReach, { light: Light; text: string }> = {
  reads: { light: 'ready', text: 'reads its vault' },
  'grant-pending': { light: 'waiting', text: 'grant pending' },
  'key-pending': { light: 'waiting', text: 'key awaiting approval' },
  none: { light: 'off', text: 'cannot read its vault' },
}

/** Whether a box can open the agent's zero-knowledge vault: a current device wrap
 *  on one of its keys, or one waiting to be granted (`zk.pending`). */
export function vaultReach(runnerId: string, zk: ZkStateOut, keys: readonly RunnerDeviceKeyOut[]): VaultReach {
  const own = keys.filter((k) => String(k.runner) === runnerId)
  const wrapped = new Set(
    zk.wraps.filter((w) => w.kind === 'device' && w.kv === zk.kv && w.current).map((w) => String(w.target)),
  )
  if (own.some((k) => k.status === 'active' && wrapped.has(String(k.id)))) return 'reads'
  if (zk.pending.some((p) => p.kind === 'device' && String(p.runner) === runnerId)) return 'grant-pending'
  if (own.some((k) => k.status === 'pending')) return 'key-pending'
  return 'none'
}

type RouteRow = { id: string; name: string; online: boolean; ready: boolean; why: string }

function useLoad<T>(load: () => Promise<T>, deps: unknown[]): T | null {
  const [value, setValue] = useState<T | null>(null)
  useEffect(() => {
    let live = true
    load().then((v) => live && setValue(v), () => live && setValue(null))
    return () => {
      live = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return value
}

function Card({ title, lede, children }: { title: string; lede: string; children: ReactNode }): JSX.Element {
  return (
    <div className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4" aria-label={title} role="group">
      <div>
        <h3 className="text-[13px] font-semibold text-foreground">{title}</h3>
        <p className="mt-0.5 text-[12px] text-muted-foreground">{lede}</p>
      </div>
      {children}
    </div>
  )
}

function Label({ children }: { children: ReactNode }): JSX.Element {
  return <h4 className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{children}</h4>
}

export function AgentHowItRuns({
  slug,
  name,
  isAdmin,
}: {
  slug: string
  name: string
  isAdmin: boolean
}): JSX.Element {
  const auth = useAuth()
  const email = auth.user?.email ?? ''
  const licenses = useLoad(() => listAgentLicenses(slug), [slug])
  const runners = useLoad(() => listRunners(), [])
  const myKeys = useLoad(() => listRunnerDeviceKeys(), [])
  const order = useLoad(() => getAgentDefaultOrder(slug), [slug])
  const rules = useLoad(() => getAgentRunnerRules(slug), [slug])
  const schedules = useLoad(() => listSchedules(slug), [slug])
  // The vault reads are its admins' alone; nobody else asks.
  const zk = useLoad(() => (isAdmin ? getZkState(slug) : Promise.reject(new Error('not admin'))), [slug, isAdmin])
  const agentKeys = useLoad(
    () => (isAdmin ? getAgentDeviceKeys(slug) : Promise.reject(new Error('not admin'))),
    [slug, isAdmin],
  )

  const using = (licenses ?? []).filter((l) => l.status === 'active')
  const requested = (licenses ?? []).filter((l) => l.status === 'requested').length
  const me = (licenses ?? []).find((l) => l.email === email)
  const readiness =
    runners && myKeys && email ? viewerReadiness({ slug, email, license: me, runners, keys: myKeys }) : null

  // The boxes it routes to: its own list (or the workspace order it follows),
  // then any box a source rule sends work to first.
  const byId = new Map((runners ?? []).map((r) => [String(r.id), r]))
  const routes: RouteRow[] = []
  for (const r of order?.runners ?? []) {
    if (!r.enabled) continue
    routes.push({ id: String(r.runner_id), name: r.runner_name, online: r.online, ready: r.ready,
                  why: order?.own ? `#${r.rank + 1}` : 'workspace order' })
  }
  for (const rule of rules ?? []) {
    const id = String(rule.runner_id)
    if (!id || routes.some((x) => x.id === id)) continue
    const box = byId.get(id)
    routes.push({ id, name: rule.runner_name, online: box?.status === 'online', ready: Boolean(box?.ready),
                  why: rule.actor ? `${rule.source}: ${rule.actor}` : rule.source || 'rule' })
  }
  const zkOn = Boolean(zk?.enabled)
  const pendingGrants = zkOn ? routes.filter((r) => vaultReach(r.id, zk!, agentKeys ?? []) === 'grant-pending').length : 0
  const enabledSchedules = (schedules ?? []).filter((s) => s.enabled).length

  return (
    <div className="grid gap-3 p-4 md:grid-cols-2" data-testid="agent-how-it-runs">
      <Card title="Use it yourself in Claude Code" lede="No runner needed. Run this once on your computer:">
        <CopyCommand command={`canopy agent use ${slug}`} testId="agent-use-command" />
        <p className="text-[12px] text-muted-foreground">
          It installs {name}&rsquo;s plugin, lets this computer read its secrets, and records your sessions as
          local turns here. Who may: its owner and admins, anyone a full: rule names, and workspace editors.
        </p>
        {readiness && (
          <p className="flex items-center gap-2 text-[12px] text-foreground" data-testid="viewer-readiness"
             data-light={readiness.light}>
            <Dot light={readiness.light} label={readiness.light} />
            {readiness.text}
          </p>
        )}
        <div>
          <Label>Using it locally</Label>
          {licenses === null ? (
            <p className="mt-1 text-[12px] text-muted-foreground">…</p>
          ) : using.length === 0 ? (
            <p className="mt-1 text-[12px] text-muted-foreground">Nobody yet.</p>
          ) : (
            <table className="mt-1 w-full text-[12px]" data-testid="local-users">
              <tbody className="divide-y divide-border">
                {using.slice(0, 5).map((l) => (
                  <tr key={l.user_id}>
                    <td className="py-1 pr-2 text-foreground" title={l.email}>{l.name}</td>
                    <td className="py-1 pr-2 text-muted-foreground">{l.private_runners?.join(', ') || '—'}</td>
                    <td className="py-1 text-right text-muted-foreground">
                      {l.last_used_at ? relativeAge(l.last_used_at) : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <a href="#operators" className="mt-1 inline-block text-[12px] text-primary hover:underline">
            {using.length > 5 ? `All ${using.length}` : 'Licences'}
            {requested > 0 ? ` (${requested} requested)` : ''} → People → Local use
          </a>
        </div>
      </Card>

      <Card title="Let it run on its own"
            lede="Needs a runner: your own Mac with the runner app (set Private, it does only your work) or a fleet cloud box. Schedules, mail and dispatches start these turns.">
        <div>
          <Label>Runners it routes to</Label>
          {order === null && rules === null ? (
            <p className="mt-1 text-[12px] text-muted-foreground">…</p>
          ) : routes.length === 0 ? (
            <p className="mt-1 text-[12px] text-muted-foreground" data-testid="no-runner">
              None. It runs only when someone uses it in Claude Code.
            </p>
          ) : (
            <table className="mt-1 w-full text-[12px]" data-testid="routed-runners">
              <tbody className="divide-y divide-border">
                {routes.map((r) => {
                  const box = byId.get(r.id)
                  const state: Light = r.online && r.ready ? 'ready' : r.online ? 'waiting' : 'off'
                  const vault = zkOn ? VAULT_LABEL[vaultReach(r.id, zk!, agentKeys ?? [])] : null
                  return (
                    <tr key={r.id} data-runner={r.name}>
                      <td className="py-1 pr-2">
                        <span className="flex items-center gap-1.5 text-foreground">
                          <Dot light={state} label={r.online ? (r.ready ? 'online, ready' : 'online, not ready') : 'offline'} />
                          {r.name}
                        </span>
                      </td>
                      <td className="py-1 pr-2 text-muted-foreground">
                        {box ? (box.private ? 'private' : 'shared') : ''}
                        {' · '}
                        {r.online ? (r.ready ? 'ready' : 'not ready') : 'offline'}
                      </td>
                      <td className="py-1 pr-2 text-muted-foreground">{r.why}</td>
                      {zkOn && (
                        <td className="py-1 text-right" data-testid={`vault-${r.name}`}>
                          <span className="inline-flex items-center gap-1.5 text-muted-foreground">
                            <Dot light={vault!.light} label={vault!.text} />
                            {vault!.text}
                          </span>
                        </td>
                      )}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
          {pendingGrants > 0 && (
            <p className="mt-1 text-[12px] text-warning" data-testid="grant-pending">
              Grant pending: {pendingGrants} runner{pendingGrants === 1 ? '' : 's'} cannot open its vault until an
              admin grants {pendingGrants === 1 ? 'it' : 'them'} access (Credentials).
            </p>
          )}
        </div>
        <p className="text-[12px] text-muted-foreground" data-testid="schedule-count">
          {schedules === null
            ? 'Schedules: …'
            : `${enabledSchedules} schedule${enabledSchedules === 1 ? '' : 's'} on. They fire from canopy itself; a runner takes the turn.`}{' '}
          <Link to="../triggers" relative="path" className="text-primary hover:underline">Triggers</Link>
          {' · '}
          <a href="#routing" className="text-primary hover:underline">Routing</a>
          {' · '}
          <a href="#credentials" className="text-primary hover:underline">Credentials</a>
        </p>
      </Card>
    </div>
  )
}
