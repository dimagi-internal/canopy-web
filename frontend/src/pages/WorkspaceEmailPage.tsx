import { useCallback, useEffect, useState, type JSX } from 'react'
import { useParams } from 'react-router-dom'

import { listAgents, type AgentOut } from '@/api/agents'
import {
  addMailDomain,
  deleteMailRoute,
  refreshMailDomain,
  removeMailDomain,
  saveMailRoute,
  updateMailDomain,
  listMailDomains,
  type MailDomainListOut,
  type MailDomainOut,
  type MailRouteOut,
} from '@/api/mail'
import { listMembers, type MemberOut } from '@/api/workspaces'

// A workspace's own email domain run through canopy (apps/mail, SES). Any member
// can read it; the server answers `can_manage` per domain, so this page never
// guesses at a role. Forwards go only to workspace members, so there is
// deliberately no free-text email field anywhere here.

const field =
  'min-h-11 rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground sm:min-h-0'
const primary =
  'min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40 sm:min-h-0'
const secondary =
  'rounded-md border border-border px-2 py-0.5 text-[12px] text-foreground hover:bg-muted disabled:opacity-40'

type Kind = 'agent' | 'forward' | 'drop'

const STATUS: Record<string, { label: string; cls: string }> = {
  pending: { label: 'Pending DNS', cls: 'text-warning' },
  verified: { label: 'Verified', cls: 'text-success' },
  suspended: { label: 'Suspended', cls: 'text-destructive' },
}

function message(e: unknown, fallback: string): string {
  return e instanceof Error ? e.message : fallback
}

function manyFound(found: readonly string[]): boolean {
  return found.filter((v) => v.toLowerCase().startsWith('v=spf1')).length > 1
}

function goesTo(r: MailRouteOut): string {
  if (r.kind === 'agent') return r.agent_slug
  if (r.kind === 'forward') return r.forward_to_email
  return 'Dropped'
}

function copy(text: string) {
  void navigator.clipboard?.writeText(text)
}

function DomainSection({ workspace, domain, agents, members, onChange, onRemoved }: {
  workspace: string
  domain: MailDomainOut
  agents: AgentOut[]
  members: MemberOut[]
  onChange: (d: MailDomainOut) => void
  onRemoved: () => void
}): JSX.Element {
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [local, setLocal] = useState('')
  const [kind, setKind] = useState<Kind>('agent')
  const [agent, setAgent] = useState('')
  const [member, setMember] = useState('')
  const [cap, setCap] = useState(String(domain.daily_send_cap))
  const manage = domain.can_manage
  const status = STATUS[domain.status] ?? { label: domain.status, cls: 'text-muted-foreground' }
  const spfMany = domain.records.some((r) => r.purpose === 'spf' && manyFound(r.found))

  async function run(fn: () => Promise<void>, fallback: string) {
    setBusy(true)
    setError(null)
    try { await fn() } catch (e: unknown) { setError(message(e, fallback)) } finally { setBusy(false) }
  }

  const reload = async () => onChange((await listMailDomains(workspace)).items.find((d) => d.id === domain.id) ?? domain)

  async function saveRoute() {
    await run(async () => {
      await saveMailRoute(workspace, domain.id, {
        local_part: local.trim(),
        kind,
        agent_slug: kind === 'agent' ? agent : '',
        forward_to_user_id: kind === 'forward' ? Number(member) : null,
        enabled: true,
      })
      setLocal('')
      await reload()
    }, 'Could not save the address')
  }

  const canSave = local.trim() !== '' && (kind === 'drop' || (kind === 'agent' ? agent !== '' : member !== ''))

  return (
    <section className="flex flex-col gap-3" data-testid={`mail-domain-${domain.id}`}>
      <div className="flex flex-wrap items-center gap-3 text-[13px]">
        <h3 className="text-[14px] font-semibold text-foreground">{domain.name}</h3>
        <span className={status.cls}>{status.label}</span>
        <span className="text-muted-foreground">Receiving: {domain.receiving ? 'live' : 'not yet'}</span>
        <button type="button" className={secondary} disabled={busy}
          onClick={() => void run(async () => onChange(await refreshMailDomain(workspace, domain.id)), 'Could not re-check')}>
          Re-check
        </button>
        {manage && (
          <button type="button" className="px-2 text-[12px] text-destructive hover:underline" disabled={busy}
            onClick={() => {
              if (!window.confirm(`Remove ${domain.name}? Its addresses stop receiving mail. Existing threads are kept.`)) return
              void run(async () => { await removeMailDomain(workspace, domain.id); onRemoved() }, 'Could not remove the domain')
            }}>
            Remove domain
          </button>
        )}
      </div>

      {!domain.receiving && domain.mx_provider && (
        <p className="text-[12px] text-warning">
          {`${domain.mx_provider} currently receives mail for this domain — switching MX moves all of it here.`}
        </p>
      )}
      {spfMany && (
        <p className="text-[12px] text-warning">
          Only one SPF record may exist on a domain. This domain has several; replace them all with the value shown below.
        </p>
      )}
      {domain.status === 'suspended' && (
        <p className="flex items-center gap-2 text-[12px] text-destructive">
          Sending suspended{domain.suspended_reason ? `: ${domain.suspended_reason}` : ''}
          {manage && (
            <button type="button" className={secondary} disabled={busy}
              onClick={() => void run(async () => onChange(await updateMailDomain(workspace, domain.id, { unsuspend: true })), 'Could not lift the suspension')}>
              Lift suspension
            </button>
          )}
        </p>
      )}

      <div>
        <h4 className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">DNS records</h4>
        <table className="mt-1 w-full text-[12px]" data-testid="mail-records">
          <thead className="text-left text-muted-foreground">
            <tr className="border-b border-border">
              <th className="py-1 pr-3 font-medium">Purpose</th>
              <th className="py-1 pr-3 font-medium">Type</th>
              <th className="py-1 pr-3 font-medium">Name</th>
              <th className="py-1 pr-3 font-medium">Value</th>
              <th className="py-1 pr-3 font-medium">Priority</th>
              <th className="py-1 font-medium">Status</th>
            </tr>
          </thead>
          <tbody>
            {domain.records.map((r, i) => (
              <tr key={`${r.purpose}-${i}`} className="border-b border-border align-top">
                <td className="py-1.5 pr-3 text-foreground">{r.purpose}</td>
                <td className="py-1.5 pr-3 text-foreground-secondary">{r.type}</td>
                <td className="break-all py-1.5 pr-3 font-mono text-foreground-secondary">{r.name}</td>
                <td className="py-1.5 pr-3">
                  <span className="break-all font-mono text-foreground">{r.value}</span>{' '}
                  <button type="button" className="text-primary hover:underline" onClick={() => copy(r.value)}
                    aria-label={`Copy ${r.purpose} value`}>Copy</button>
                </td>
                <td className="py-1.5 pr-3 text-foreground-secondary">{r.priority ?? ''}</td>
                <td className="py-1.5">
                  {r.ok ? <span className="text-success">✓</span> : <span className="text-destructive">✗</span>}
                  {!r.ok && r.found.length > 0 && (
                    <span className="ml-1 break-all text-muted-foreground">found: {r.found.join(', ')}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div>
        <h4 className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Addresses</h4>
        {domain.routes.length === 0 ? (
          <p className="mt-1 text-[12px] text-muted-foreground">No addresses yet.</p>
        ) : (
          <table className="mt-1 w-full text-[12px]" data-testid="mail-routes">
            <thead className="text-left text-muted-foreground">
              <tr className="border-b border-border">
                <th className="py-1 pr-3 font-medium">Address</th>
                <th className="py-1 pr-3 font-medium">Goes to</th>
                <th className="py-1 pr-3 font-medium">Enabled</th>
                {manage && <th className="py-1" />}
              </tr>
            </thead>
            <tbody>
              {domain.routes.map((r) => (
                <tr key={r.id} className="border-b border-border">
                  <td className="py-1.5 pr-3 text-foreground">{r.address}</td>
                  <td className="py-1.5 pr-3 text-foreground-secondary">{goesTo(r)}</td>
                  <td className="py-1.5 pr-3 text-foreground-secondary">{r.enabled ? 'Yes' : 'No'}</td>
                  {manage && (
                    <td className="whitespace-nowrap py-1.5 text-right">
                      <button type="button" className="px-2 text-primary hover:underline"
                        onClick={() => {
                          setLocal(r.local_part); setKind(r.kind as Kind); setAgent(r.agent_slug)
                          setMember(r.forward_to_user_id ? String(r.forward_to_user_id) : '')
                        }}>Edit</button>
                      <button type="button" className="px-2 text-destructive hover:underline" disabled={busy}
                        onClick={() => {
                          if (!window.confirm(`Remove ${r.address}? It stops receiving mail. Existing threads are kept.`)) return
                          void run(async () => { await deleteMailRoute(workspace, domain.id, r.id); await reload() }, 'Could not remove the address')
                        }}>
                        Remove
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {manage && (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <input aria-label="Local part" placeholder="jjackson, or * for everything else" value={local}
              onChange={(e) => setLocal(e.target.value)} className={`${field} w-56`} />
            <select aria-label="Route kind" className={field} value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
              <option value="agent">Agent</option>
              <option value="forward">Forward to member</option>
              <option value="drop">Drop</option>
            </select>
            {kind === 'agent' && (
              <select aria-label="Agent" className={field} value={agent} onChange={(e) => setAgent(e.target.value)}>
                <option value="">Choose an agent</option>
                {agents.map((a) => <option key={a.slug} value={a.slug}>{a.name}</option>)}
              </select>
            )}
            {kind === 'forward' && (
              <select aria-label="Member" className={field} value={member} onChange={(e) => setMember(e.target.value)}>
                <option value="">Choose a member</option>
                {members.map((m) => <option key={m.user_id} value={m.user_id}>{`${m.email} (${m.role})`}</option>)}
              </select>
            )}
            <button type="button" className={primary} disabled={busy || !canSave} onClick={() => void saveRoute()}>
              Save address
            </button>
          </div>
        )}
      </div>

      {manage && (
        <label className="flex items-center gap-2 text-[12px] text-muted-foreground">
          Daily send cap
          <input type="number" min={0} aria-label="Daily send cap" value={cap}
            onChange={(e) => setCap(e.target.value)} className={`${field} w-24`} />
          <button type="button" className={secondary} disabled={busy || cap === '' || Number(cap) === domain.daily_send_cap}
            onClick={() => void run(async () => onChange(await updateMailDomain(workspace, domain.id, { daily_send_cap: Number(cap) })), 'Could not save the cap')}>
            Save
          </button>
        </label>
      )}

      {error && <p className="text-[12px] text-destructive" data-testid="mail-error">{error}</p>}
    </section>
  )
}

export function WorkspaceEmailPage(): JSX.Element {
  const { workspace: slug = '' } = useParams()
  const [list, setList] = useState<MailDomainListOut | null>(null)
  const [agents, setAgents] = useState<AgentOut[]>([])
  const [members, setMembers] = useState<MemberOut[]>([])
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    listMailDomains(slug).then(setList).catch((e: unknown) => setError(message(e, 'Could not load email')))
  }, [slug])

  useEffect(() => {
    load()
    listAgents().then((p) => setAgents(p.items.filter((a) => a.workspace === slug))).catch(() => {})
    listMembers(slug).then(setMembers).catch(() => {})
  }, [slug, load])

  async function add() {
    setBusy(true)
    setError(null)
    try {
      await addMailDomain(slug, name.trim().toLowerCase())
      setName('')
      load()
    } catch (e: unknown) {
      setError(message(e, 'Could not add the domain'))
    } finally {
      setBusy(false)
    }
  }

  if (!list) {
    return error
      ? <p className="text-[13px] text-destructive" data-testid="mail-error">{error}</p>
      : <p className="text-[13px] text-muted-foreground">Loading…</p>
  }

  const canAdd = list.items.length === 0 || list.items.some((d) => d.can_manage)

  return (
    <div className="flex max-w-5xl flex-col gap-5" data-testid="workspace-email">
      <section>
        <h2 className="text-[15px] font-semibold text-foreground">Email</h2>
        <p className="mt-1 max-w-3xl text-[13px] text-foreground-secondary">
          Run your own domain&rsquo;s email through canopy. Publish the DNS records below, then choose where
          each address goes: an agent, a member of this workspace, or nowhere.
        </p>
      </section>

      {!list.enabled ? (
        <p className="text-[13px] text-muted-foreground">Email domains are off on this deployment.</p>
      ) : (
        <>
          {list.items.length === 0 && canAdd && (
            <div className="flex flex-wrap items-center gap-2">
              <input aria-label="Domain" placeholder="example.com" value={name}
                onChange={(e) => setName(e.target.value)} className={`${field} w-64`} />
              <button type="button" className={primary} disabled={busy || name.trim() === ''} onClick={() => void add()}>
                Add domain
              </button>
            </div>
          )}
          {list.items.map((d) => (
            <DomainSection key={d.id} workspace={slug} domain={d} agents={agents} members={members}
              onChange={(next) => setList({ ...list, items: list.items.map((x) => (x.id === next.id ? next : x)) })}
              onRemoved={load} />
          ))}
          {error && <p className="text-[12px] text-destructive" data-testid="mail-error">{error}</p>}
        </>
      )}
    </div>
  )
}

export default WorkspaceEmailPage
