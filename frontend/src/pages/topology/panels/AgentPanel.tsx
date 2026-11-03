import { type JSX } from 'react'
import { Link } from 'react-router-dom'
import { clsx } from 'clsx'
import { type TurnMode } from '@/api/agents'
import { AgentRouting } from '@/components/agents/AgentRouting'
import { type AgentEdgeOut } from '@/api/workspaces'
import { edgeKey, explainEdge } from '@/pages/agentTopology'
import { type FleetMap, type MapAgent } from '@/pages/topology/topologyMap'
import { scopedPath } from '@/lib/scopedLinks'
import { PanelHeading } from '@/pages/topology/panels/shared'
import { type Selection } from '@/pages/topology/map/shared'

export function AgentPanel({
  agent,
  map,
  busy,
  onSelect,
  onGrant,
  onRevoke,
  onRoutingSaved,
}: {
  agent: MapAgent
  map: FleetMap
  busy: boolean
  onSelect: (s: Selection) => void
  onGrant: (e: AgentEdgeOut) => void
  onRevoke: (e: AgentEdgeOut) => void
  onRoutingSaved: () => void
}): JSX.Element {
  const others = [...map.agents.values()].filter((a) => a.slug !== agent.slug)
  const rank = { full: 0, confined: 1, none: 2 } as const
  const outgoing = others
    .map((a) => ({ a, e: map.edges.get(edgeKey(agent.slug, a.slug)) }))
    .filter((x): x is { a: MapAgent; e: AgentEdgeOut } => Boolean(x.e))
    .sort((x, y) => rank[x.e.access] - rank[y.e.access] || x.a.name.localeCompare(y.a.name))
  const incoming = others
    .map((a) => ({ a, e: map.edges.get(edgeKey(a.slug, agent.slug)) }))
    .filter((x): x is { a: MapAgent; e: AgentEdgeOut } => Boolean(x.e) && x.e!.basis !== 'no-login')
  const src = map.profiles.get(agent.slug)
  return (
    <>
      <div className="flex flex-col gap-1">
        <div className="flex items-baseline justify-between gap-2">
          <h3 className="m-0 text-[18px] font-semibold text-foreground">{agent.name}</h3>
          <Link to={scopedPath(agent.workspace, `/agents/${agent.slug}/settings`)} className="text-[12px] text-primary hover:underline">
            Settings →
          </Link>
        </div>
        <span className="text-[12px] text-muted-foreground">
          {agent.workspace} · {agent.ownerEmail ? `owner ${agent.ownerEmail}` : 'no owner set'}
        </span>
        <span className="text-[12px] text-muted-foreground">
          {agent.loginEmail ? `signs in as ${agent.loginEmail}` : 'no canopy login — it cannot send other agents work'}
        </span>
      </div>

      {/* The agent's own routing table, the one on its Settings page — edited
          here, redrawn on the map (its runner chips number themselves). */}
      <section className="flex flex-col gap-1.5" data-testid="map-agent-routing">
        <PanelHeading>Where its work runs</PanelHeading>
        {agent.health !== 'ok' && (
          <span className="text-[12px] text-destructive">
            {agent.health === 'unrouted' ? 'No runner: its turns cannot run.' : 'None of its runners is live.'}
          </span>
        )}
        <AgentRouting
          key={agent.slug}
          agentSlug={agent.slug}
          agentName={agent.name}
          workspace={agent.workspace}
          initialTurnMode={(agent.turnMode || 'manual') as TurnMode}
          onSaved={onRoutingSaved}
        />
      </section>

      <section className="flex flex-col gap-1.5">
        <PanelHeading>When {agent.name} sends work to…</PanelHeading>
        {!agent.loginEmail && <span className="text-muted-foreground">Nobody: it has no login to send with.</span>}
        {agent.loginEmail &&
          outgoing.map(({ a, e }) => {
            const dst = map.profiles.get(a.slug)
            return (
              <div
                key={a.slug}
                className={clsx(
                  'flex flex-col gap-1.5 rounded-md px-1 py-1',
                  e.access === 'confined' && 'border border-warning/30 bg-warning/10 p-2',
                )}
              >
                <div className="flex items-baseline justify-between gap-2">
                  <button type="button" onClick={() => onSelect({ kind: 'agent', slug: a.slug })} className="font-medium text-foreground hover:underline">
                    {a.name}
                  </button>
                  <span className={clsx('text-[12px]', e.access === 'full' ? 'text-success' : e.access === 'confined' ? 'text-warning' : 'text-muted-foreground')}>
                    {e.access === 'full' ? 'All' : e.access === 'confined' ? e.capabilities.join(', ') + ' only' : 'None'}
                  </span>
                </div>
                {e.access !== 'full' && src && dst && (
                  <span className="text-[12px] text-foreground-secondary">{explainEdge(e, src, dst)}</span>
                )}
                {e.can_grant && e.access !== 'full' && (
                  <>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => onGrant(e)}
                      className="min-h-9 self-start rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-40"
                      data-testid={`map-grant-${a.slug}`}
                    >
                      Make {agent.name} an admin of {a.name}
                    </button>
                    <span className="text-[11px] text-muted-foreground">
                      Then {agent.fullPeople.length} {agent.fullPeople.length === 1 ? 'person' : 'people'} who get all of {agent.name} can steer {a.name} through it.
                    </span>
                  </>
                )}
                {e.can_revoke && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => onRevoke(e)}
                    className="self-start text-[12px] text-destructive hover:underline disabled:opacity-40"
                  >
                    Revoke admin
                  </button>
                )}
              </div>
            )
          })}
      </section>

      <section className="flex flex-col gap-1">
        <PanelHeading>Who can send {agent.name} work</PanelHeading>
        {incoming.length === 0 && <span className="text-muted-foreground">No other agent with a login.</span>}
        {incoming.map(({ a, e }) => (
          <div key={a.slug} className="flex items-baseline justify-between gap-2">
            <button type="button" onClick={() => onSelect({ kind: 'agent', slug: a.slug })} className="text-foreground-secondary hover:underline">
              {a.name}
            </button>
            <span className={clsx('text-[12px]', e.access === 'full' ? 'text-success' : e.access === 'confined' ? 'text-warning' : 'text-muted-foreground')}>
              {e.access === 'full' ? 'All' : e.access === 'confined' ? e.capabilities.join(', ') + ' only' : e.basis === 'not-member' ? `None · not in ${agent.workspace}` : 'None'}
            </span>
          </div>
        ))}
      </section>
    </>
  )
}
