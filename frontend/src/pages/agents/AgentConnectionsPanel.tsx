import { useCallback, useEffect, useState } from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'
import {
  connectionStartAction,
  disconnectAgentConnection,
  grantConnection,
  listAgentConnections,
  revokeConnectionGrant,
  type ConnectionOut,
  type ConnectionsView,
} from '@/api/connections'
import { listAgents } from '@/api/agents'
import { getCsrfToken } from '@/api/base'
import { ConnectOutcome, Scopes, lastUsed } from '@/components/settings/ConnectionsPanel'
import { Button } from 'canopy-ui/ui'

/**
 * What an agent can act as through canopy-held connections: its own, and ones
 * another agent shares with it. Admin-only (the server enforces it too).
 *
 * Disconnecting asks for a fresh passkey; the API client's step-up middleware
 * prompts and retries, so this just calls DELETE.
 *
 * Connecting is a form POST (CSRF-checked), never a link: the server refuses a
 * GET that names an agent, and makes Salesforce ask for a login, so the account
 * attached is the one the admin types in — the agent's — not whichever
 * Salesforce session their browser happens to hold.
 */
export function AgentConnectionsPanel({ agent }: { agent: { slug: string; name?: string } }) {
  const [data, setData] = useState<ConnectionsView | null>(null)
  const [others, setOthers] = useState<string[]>([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<number | null>(null)
  const [pick, setPick] = useState<Record<number, string>>({})
  const [notice, setNotice] = useState('')
  const { pathname } = useLocation()
  const [params, setParams] = useSearchParams()
  const outcome = params.get('connection') ? params : null

  const load = useCallback(() => {
    listAgentConnections(agent.slug).then((d) => { setData(d); setError('') }).catch(() => setError('Could not read the agent’s connections.'))
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

  async function run(id: number, what: () => Promise<void>, done: string) {
    setBusy(id)
    setError('')
    setNotice('')
    try {
      await what()
      setNotice(done)
      setPick((s) => ({ ...s, [id]: '' }))
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
      <ConnectOutcome params={outcome} onDismiss={dismiss} knownProviders={data.providers} />
      {error && <p className="text-[13px] text-destructive">{error}</p>}
      {notice && <p role="status" className="text-[13px] text-success">{notice}</p>}
      {data.providers.map((p) => {
        const rows = data.connections.filter((c) => c.provider === p.name)
        const own = rows.filter((c) => !c.granted_from)
        const start = { action: connectionStartAction(p.name), returnTo: pathname, agent: agent.slug }
        return (
          <div key={p.name} className="space-y-2" data-testid={`agent-connection-provider-${p.name}`}>
            <p className="text-[11px] uppercase tracking-wider font-semibold text-muted-foreground">{p.title}</p>
            {rows.map((c) => (
              <Row
                key={c.id}
                c={c}
                start={start}
                busy={busy === c.id}
                others={others}
                picked={pick[c.id] ?? ''}
                onPick={(v) => setPick((s) => ({ ...s, [c.id]: v }))}
                onDisconnect={() => run(c.id, () => disconnectAgentConnection(agent.slug, c.id), 'Disconnected.')}
                onGrant={(to) => run(c.id, () => grantConnection(agent.slug, c.id, to), `Shared with ${to}.`)}
                onRevoke={(to) => run(c.id, () => revokeConnectionGrant(c.granted_from ?? agent.slug, c.id, to), c.granted_from ? 'Stopped using it.' : `Stopped sharing with ${to}.`)}
                self={agent.slug}
              />
            ))}
            {!p.configured ? (
              <div>
                <Button size="sm" disabled>Connect {p.title}</Button>
                <p className="mt-1 text-[12px] text-muted-foreground">Not set up on this deployment</p>
              </div>
            ) : own.length === 0 ? (
              <ConnectForm label={`Connect ${p.title}`} {...start} />
            ) : null}
          </div>
        )
      })}
    </div>
  )
}

interface StartForm {
  action: string
  returnTo: string
  agent: string
}

/** Starts an agent's connect: a full-page form POST carrying the CSRF token. */
function ConnectForm({ label, action, returnTo, agent }: { label: string } & StartForm) {
  return (
    <form method="post" action={action} className="inline-block">
      <input type="hidden" name="csrfmiddlewaretoken" value={getCsrfToken()} />
      <input type="hidden" name="return_to" value={returnTo} />
      <input type="hidden" name="agent" value={agent} />
      <Button type="submit" size="sm">{label}</Button>
    </form>
  )
}

function Row({
  c, start, busy, others, picked, onPick, onDisconnect, onGrant, onRevoke, self,
}: {
  c: ConnectionOut
  start: StartForm
  busy: boolean
  others: string[]
  picked: string
  onPick: (v: string) => void
  onDisconnect: () => void
  onGrant: (to: string) => void
  onRevoke: (to: string) => void
  self: string
}) {
  const shared = !!c.granted_from
  const sharedWith = c.shared_with ?? []
  const candidates = others.filter((s) => s !== self && !sharedWith.includes(s))
  return (
    <div className="space-y-2 rounded border border-border bg-muted/40 p-2.5" data-testid={`agent-connection-${c.id}`}>
      <div className="flex items-center justify-between gap-4">
        <div className="text-[13px]">
          <p className="font-medium text-foreground">{c.account_label}</p>
          <p className="text-[12px] text-muted-foreground">
            {c.instance_url} · {lastUsed(c)}
            {shared && <> · shared by {c.granted_from}</>}
          </p>
          <Scopes c={c} />
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
              {c.needs_reconnect && <ConnectForm label="Reconnect" {...start} />}
              <Button size="sm" variant="outline" disabled={busy} onClick={onDisconnect}>
                {busy ? 'Working…' : 'Disconnect'}
              </Button>
            </>
          )}
        </div>
      </div>
      {!shared && sharedWith.length > 0 && (
        <ul className="flex flex-wrap items-center gap-2 text-[12px]" aria-label="Shared with">
          {sharedWith.map((to) => (
            <li key={to} className="flex items-center gap-1 rounded-full border border-border bg-background px-2 py-0.5">
              <span>{to}</span>
              <button
                type="button"
                disabled={busy}
                aria-label={`Stop sharing with ${to}`}
                onClick={() => onRevoke(to)}
                className="text-muted-foreground hover:text-foreground"
              >
                Stop sharing
              </button>
            </li>
          ))}
        </ul>
      )}
      {!shared && candidates.length > 0 && (
        <div className="flex items-center gap-2 text-[12px]">
          <label htmlFor={`share-${c.id}`} className="text-muted-foreground">Share with</label>
          <select
            id={`share-${c.id}`}
            value={picked}
            onChange={(e) => onPick(e.target.value)}
            className="rounded border border-border bg-background px-2 py-1"
          >
            <option value="">Choose an agent…</option>
            {candidates.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <Button size="sm" variant="outline" disabled={busy || !picked} onClick={() => onGrant(picked)}>
            Share
          </Button>
        </div>
      )}
    </div>
  )
}
