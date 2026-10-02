import { useCallback, useEffect, useMemo, useState, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'
import { clsx } from 'clsx'

import { grantAgentAdmin, revokeAgentAdmin } from '@/api/agents'
import {
  getAgentTopology,
  WorkspaceApiError,
  type AgentEdgeOut,
  type AgentTopologyAgentOut,
  type AgentTopologyOut,
} from '@/api/workspaces'
import { edgeKey, explainEdge, grantable, indexEdges } from './agentTopology'

// THE AGENT TOPOLOGY: which agent can make which other agent do what.
//
// An agent sends another agent work with its own canopy login, and to the
// receiving agent that login is just a person — owner, admin, a member its
// interface confines, or nobody. Whether Ada could send Hal work was spread
// across Hal's interface, Hal's admin list and two workspaces' member lists,
// and the answer ("ask" only — which needs an email thread) left every direct
// Ada→Hal dispatch failing for a week while each of those screens looked fine.
//
// The one-click fix is making the sender's login an admin of the receiver, and
// that grant is TRANSITIVE: anyone with the sender's whole profile can then
// steer the receiver through it. So the grant says who that is before it is made.

const CELL = {
  full: { glyph: '●', className: 'text-success', label: 'Full' },
  confined: { glyph: '◐', className: 'text-warning', label: 'Confined' },
  none: { glyph: '○', className: 'text-muted-foreground', label: 'None' },
} as const

export function WorkspaceAgentAccessPage(): JSX.Element {
  const { workspace: slug = '' } = useParams()
  return <AgentAccess key={slug} slug={slug} />
}

function AgentAccess({ slug }: { slug: string }): JSX.Element {
  const [topo, setTopo] = useState<AgentTopologyOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const [picked, setPicked] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const load = useCallback(() => getAgentTopology(slug), [slug])

  useEffect(() => {
    let off = false
    load()
      .then((t) => { if (!off) setTopo(t) })
      .catch((e: unknown) => {
        if (off) return
        if (e instanceof WorkspaceApiError && (e.status === 403 || e.status === 404)) setForbidden(true)
        else setError(e instanceof Error ? e.message : 'Could not load the agent topology')
      })
    return () => { off = true }
  }, [load])

  const edges = useMemo(() => indexEdges(topo?.edges ?? []), [topo])
  const agents = useMemo(() => new Map((topo?.agents ?? []).map((a) => [a.slug, a] as const)), [topo])

  async function act(run: () => Promise<unknown>) {
    setBusy(true)
    setActionError(null)
    try {
      await run()
      setTopo(await load())
    } catch (e: unknown) {
      setActionError(e instanceof Error ? e.message : 'That did not work')
    } finally {
      setBusy(false)
    }
  }

  if (forbidden) {
    return (
      <p className="text-[13px] text-muted-foreground" data-testid="agent-topology-forbidden">
        Only a workspace admin or owner can see the agent topology.
      </p>
    )
  }
  if (error) return <p className="text-[13px] text-destructive">{error}</p>
  if (!topo) return <p className="text-[13px] text-muted-foreground">Loading…</p>

  const list = topo.agents
  const fixable = grantable(topo.edges)
  const pickedEdge = picked ? edges.get(picked) : undefined

  function grantAll() {
    const exposed = new Set(fixable.flatMap((e) => agents.get(e.source)?.full_people ?? []))
    const ok = window.confirm(
      `Make ${fixable.length} agent login${fixable.length === 1 ? '' : 's'} admins of the agents they cannot fully reach?\n\n` +
        `Anyone with a sender's whole profile can then steer the receiver through it — ${exposed.size} ` +
        `${exposed.size === 1 ? 'person' : 'people'}: ${[...exposed].join(', ') || 'nobody beyond the owners'}.`,
    )
    if (!ok) return
    void act(async () => {
      for (const e of fixable) {
        const src = agents.get(e.source)
        if (src?.login_user_id != null) await grantAgentAdmin(e.target, src.login_user_id, agents.get(e.target)?.workspace)
      }
    })
  }

  return (
    <div className="flex flex-col gap-5" data-testid="agent-topology">
      <section>
        <h2 className="text-[15px] font-semibold text-foreground">
          Agent topology{' '}
          <span className="text-muted-foreground">
            · {list.length} agent{list.length === 1 ? '' : 's'} across {topo.workspaces.length} workspace
            {topo.workspaces.length === 1 ? '' : 's'}
          </span>
        </h2>
        <p className="mt-1 max-w-3xl text-[13px] text-foreground-secondary">
          What each agent gets when it sends another agent work with its own canopy login. To the receiving
          agent that login is just a person: its owner and admins get the whole agent; a member gets what
          the agent&rsquo;s published interface allows; anyone outside its workspace gets nothing. Rows send,
          columns receive. Select a cell to see why, and to grant access.
        </p>
      </section>

      {list.length < 2 ? (
        <p className="text-[12px] text-muted-foreground">Fewer than two agents here — nothing to connect.</p>
      ) : (
        <section>
          <div className="mb-2 flex flex-wrap items-center gap-3 text-[12px] text-muted-foreground">
            {Object.values(CELL).map((c) => (
              <span key={c.label}>
                <span className={c.className}>{c.glyph}</span> {c.label}
              </span>
            ))}
            {fixable.length > 0 && (
              <button
                type="button"
                disabled={busy}
                onClick={grantAll}
                data-testid="grant-all"
                className="ml-auto min-h-11 rounded-md border border-border px-3 py-1 text-[12px] text-foreground hover:bg-muted disabled:opacity-40 sm:min-h-0"
              >
                Make agents admins of each other ({fixable.length})
              </button>
            )}
          </div>
          <div className="overflow-x-auto">
            <table className="text-[12px]" data-testid="agent-matrix">
              <thead>
                <tr className="border-b border-border">
                  <th className="py-1.5 pr-3 text-left font-medium text-muted-foreground">sends ↓ · receives →</th>
                  {list.map((dst) => (
                    <th key={dst.slug} className="px-2 py-1.5 text-center font-medium text-foreground">
                      <Link to={`/w/${dst.workspace}/agents/${dst.slug}/settings`} className="hover:underline">
                        {dst.name}
                      </Link>
                      <div className="font-normal text-muted-foreground">{dst.workspace}</div>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {list.map((src) => (
                  <tr key={src.slug} className={clsx('border-b border-border', !src.login_email && 'opacity-50')}>
                    <th className="py-1.5 pr-3 text-left font-normal">
                      <span className="font-medium text-foreground">{src.name}</span>{' '}
                      <span className="text-muted-foreground">{src.workspace}</span>
                      <div className="text-[11px] text-muted-foreground">{src.login_email ?? 'no login'}</div>
                    </th>
                    {list.map((dst) => {
                      if (src.slug === dst.slug) {
                        return <td key={dst.slug} className="px-2 text-center text-foreground-subtle">—</td>
                      }
                      const key = edgeKey(src.slug, dst.slug)
                      const e = edges.get(key)
                      if (!e) return <td key={dst.slug} />
                      const c = CELL[e.access]
                      return (
                        <td key={dst.slug} className="px-1 text-center">
                          <button
                            type="button"
                            onClick={() => setPicked(picked === key ? null : key)}
                            title={`${src.name} → ${dst.name}: ${c.label}${e.capabilities.length ? ` (${e.capabilities.join(', ')})` : ''}`}
                            data-testid={`cell-${src.slug}-${dst.slug}`}
                            aria-pressed={picked === key}
                            className={clsx(
                              'min-h-9 min-w-9 rounded px-2 py-1 hover:bg-muted',
                              picked === key && 'bg-primary/10 ring-1 ring-primary',
                            )}
                          >
                            <span className={c.className}>{c.glyph}</span>
                            {e.access === 'confined' && (
                              <span className="ml-1 text-[10px] text-muted-foreground">{e.capabilities.join(',')}</span>
                            )}
                          </button>
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      {pickedEdge && agents.get(pickedEdge.source) && agents.get(pickedEdge.target) && (
        <EdgeDetail
          edge={pickedEdge}
          src={agents.get(pickedEdge.source)!}
          dst={agents.get(pickedEdge.target)!}
          busy={busy}
          onGrant={(e, src) => void act(() => grantAgentAdmin(e.target, src.login_user_id!, agents.get(e.target)?.workspace))}
          onRevoke={(e, src) => void act(() => revokeAgentAdmin(e.target, src.login_user_id!, agents.get(e.target)?.workspace))}
        />
      )}
      {actionError && <p className="text-[12px] text-destructive">{actionError}</p>}
    </div>
  )
}

function EdgeDetail({
  edge,
  src,
  dst,
  busy,
  onGrant,
  onRevoke,
}: {
  edge: AgentEdgeOut
  src: AgentTopologyAgentOut
  dst: AgentTopologyAgentOut
  busy: boolean
  onGrant: (e: AgentEdgeOut, src: AgentTopologyAgentOut) => void
  onRevoke: (e: AgentEdgeOut, src: AgentTopologyAgentOut) => void
}): JSX.Element {
  const c = CELL[edge.access]
  return (
    <section className="max-w-3xl rounded-lg border border-border bg-card p-3" data-testid="edge-detail">
      <h3 className="text-[13px] font-semibold text-foreground">
        {src.name} → {dst.name}: <span className={c.className}>{c.label}</span>
      </h3>
      <p className="mt-1 text-[13px] text-foreground-secondary">{explainEdge(edge, src, dst)}</p>
      {edge.basis === 'not-member' && (
        <Link to={`/w/${dst.workspace}/settings/members`} className="mt-2 inline-block text-[12px] text-primary hover:underline">
          {dst.workspace} members →
        </Link>
      )}
      {edge.can_grant && src.login_user_id != null && (
        <div className="mt-3 flex flex-col gap-2">
          <p className="text-[12px] text-muted-foreground">
            Making {src.login_email} an admin of {dst.name} gives {src.name} all of {dst.name}
            {edge.access === 'full' ? ', and keeps it if an interface is published later' : ''}. Anyone with all of{' '}
            {src.name} could then steer {dst.name} through it:{' '}
            <span className="text-foreground-secondary">{src.full_people.join(', ') || 'nobody beyond its owners'}</span>.
          </p>
          <button
            type="button"
            disabled={busy}
            onClick={() => onGrant(edge, src)}
            data-testid="grant-edge"
            className="min-h-11 self-start rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40 sm:min-h-0"
          >
            Make {src.name} an admin of {dst.name}
          </button>
        </div>
      )}
      {edge.can_revoke && src.login_user_id != null && (
        <button
          type="button"
          disabled={busy}
          onClick={() => onRevoke(edge, src)}
          data-testid="revoke-edge"
          className="mt-3 min-h-11 rounded-md border border-border px-3 py-1 text-[12px] text-destructive hover:bg-muted disabled:opacity-40 sm:min-h-0"
        >
          Revoke {src.name}&rsquo;s admin on {dst.name}
        </button>
      )}
      {!edge.can_grant && !edge.can_revoke && edge.basis !== 'not-member' && edge.basis !== 'no-login' && edge.access !== 'full' && (
        <p className="mt-2 text-[12px] text-muted-foreground">
          Only {dst.name}&rsquo;s owner or a workspace owner can grant this.
        </p>
      )}
    </section>
  )
}
