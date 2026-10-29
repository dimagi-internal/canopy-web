import { useCallback, useEffect, useState } from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'
import {
  connectionStartPath,
  disconnectAgentConnection,
  grantConnection,
  listAgentConnections,
  revokeConnectionGrant,
  type ConnectionOut,
  type ConnectionsView,
} from '@/api/connections'
import { listAgents } from '@/api/agents'
import { ConnectLink, ConnectOutcome, lastUsed } from '@/components/settings/ConnectionsPanel'
import { Button } from 'canopy-ui/ui'

/**
 * What an agent can act as through canopy-held connections: its own, and ones
 * another agent shares with it. Admin-only (the server enforces it too).
 *
 * Disconnecting asks for a fresh passkey; the API client's step-up middleware
 * prompts and retries, so this just calls DELETE.
 */
export function AgentConnectionsPanel({ agent }: { agent: { slug: string; name?: string } }) {
  const [data, setData] = useState<ConnectionsView | null>(null)
  const [others, setOthers] = useState<string[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<number | null>(null)
  const [pick, setPick] = useState<Record<number, string>>({})
  const { pathname } = useLocation()
  const [params, setParams] = useSearchParams()
  const outcome = params.get('connection') ? params : null

  const load = useCallback(() => {
    listAgentConnections(agent.slug).then(setData).catch(() => setError('Could not read the agent’s connections.'))
  }, [agent.slug])
  useEffect(load, [load, outcome?.get('status')])
  useEffect(() => {
    listAgents({ limit: 200 })
      .then((p) => setOthers(p.items.map((a) => a.slug).filter((s) => s !== agent.slug)))
      .catch(() => setOthers([]))
  }, [agent.slug])

  function dismiss() {
    const next = new URLSearchParams(params)
    for (const k of ['connection', 'status', 'reason']) next.delete(k)
    setParams(next, { replace: true })
  }

  async function run(id: number, what: () => Promise<void>) {
    setBusy(id)
    setError('')
    try {
      await what()
      load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'That did not work.')
    } finally {
      setBusy(null)
    }
  }

  if (data === null) return error ? <p className="text-[13px] text-destructive">{error}</p> : null

  return (
    <div className="space-y-4" data-testid="agent-connections">
      <ConnectOutcome params={outcome} onDismiss={dismiss} />
      {error && <p className="text-[13px] text-destructive">{error}</p>}
      {data.providers.map((p) => {
        const rows = data.connections.filter((c) => c.provider === p.name)
        const own = rows.filter((c) => !c.granted_from)
        const href = connectionStartPath(p.name, pathname, agent.slug)
        return (
          <div key={p.name} className="space-y-2" data-testid={`agent-connection-provider-${p.name}`}>
            <p className="text-[11px] uppercase tracking-wider font-semibold text-muted-foreground">{p.title}</p>
            {rows.map((c) => (
              <Row
                key={c.id}
                c={c}
                href={href}
                busy={busy === c.id}
                others={others}
                picked={pick[c.id] ?? ''}
                onPick={(v) => setPick((s) => ({ ...s, [c.id]: v }))}
                onDisconnect={() => run(c.id, () => disconnectAgentConnection(agent.slug, c.id))}
                onGrant={(to) => run(c.id, () => grantConnection(agent.slug, c.id, to))}
                onRevoke={(to, owned) => run(c.id, () => revokeConnectionGrant(owned ? agent.slug : (c.granted_from ?? agent.slug), c.id, to))}
                self={agent.slug}
              />
            ))}
            {!p.configured ? (
              <div>
                <Button size="sm" disabled>Connect {p.title}</Button>
                <p className="mt-1 text-[12px] text-muted-foreground">Not set up on this deployment</p>
              </div>
            ) : own.length === 0 ? (
              <ConnectLink label={`Connect ${p.title}`} href={href} />
            ) : null}
          </div>
        )
      })}
    </div>
  )
}

function Row({
  c, href, busy, others, picked, onPick, onDisconnect, onGrant, onRevoke, self,
}: {
  c: ConnectionOut
  href: string
  busy: boolean
  others: string[]
  picked: string
  onPick: (v: string) => void
  onDisconnect: () => void
  onGrant: (to: string) => void
  onRevoke: (to: string, owned?: boolean) => void
  self: string
}) {
  const shared = !!c.granted_from
  return (
    <div className="space-y-2 rounded border border-border bg-muted/40 p-2.5" data-testid={`agent-connection-${c.id}`}>
      <div className="flex items-center justify-between gap-4">
        <div className="text-[13px]">
          <p className="font-medium text-foreground">{c.account_label}</p>
          <p className="text-[12px] text-muted-foreground">
            {c.instance_url} · {lastUsed(c)}
            {shared && <> · shared by {c.granted_from}</>}
          </p>
          {c.needs_reconnect && (
            <p className="text-[12px] text-warning">
              {shared ? 'This connection needs reconnecting by its owner.' : 'This connection expired or was revoked. Reconnect it.'}
            </p>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {shared ? (
            <Button size="sm" variant="outline" disabled={busy} onClick={() => onRevoke(self)}>
              Stop using
            </Button>
          ) : (
            <>
              {c.needs_reconnect && <ConnectLink label="Reconnect" href={href} />}
              <Button size="sm" variant="outline" disabled={busy} onClick={onDisconnect}>
                {busy ? 'Working…' : 'Disconnect'}
              </Button>
            </>
          )}
        </div>
      </div>
      {!shared && others.length > 0 && (
        <div className="flex items-center gap-2 text-[12px]">
          <label htmlFor={`share-${c.id}`} className="text-muted-foreground">Share with</label>
          <select
            id={`share-${c.id}`}
            value={picked}
            onChange={(e) => onPick(e.target.value)}
            className="rounded border border-border bg-background px-2 py-1"
          >
            <option value="">Choose an agent…</option>
            {others.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <Button size="sm" variant="outline" disabled={busy || !picked} onClick={() => onGrant(picked)}>
            Share
          </Button>
          <Button size="sm" variant="outline" disabled={busy || !picked} onClick={() => onRevoke(picked, true)}>
            Stop sharing
          </Button>
        </div>
      )}
    </div>
  )
}
