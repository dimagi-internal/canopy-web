import { useCallback, useEffect, useState } from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'
import {
  connectionsUnder,
  connectionStartPath,
  disconnectMine,
  listMyConnections,
  type ConnectionOut,
  type ConnectionsView,
  type ProviderOut,
} from '@/api/connections'
import { Button } from 'canopy-ui/ui'

/**
 * Connect the services canopy acts on for you: one section per provider the API
 * lists (`providers`), nothing hard-coded here. Same shape as the GitHub panel:
 * a per-user grant canopy holds, started by a full-page navigation to the
 * provider and reported back through the query string.
 */
export function ConnectionsPanel() {
  const [data, setData] = useState<ConnectionsView | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<number | null>(null)
  const { pathname } = useLocation()
  const [params, setParams] = useSearchParams()
  const outcome = params.get('connection') ? params : null

  const load = useCallback(() => {
    listMyConnections().then((d) => { setData(d); setError('') }).catch(() => setError('Could not read your connections.'))
  }, [])
  useEffect(load, [load, outcome?.get('status')])

  function dismiss() {
    const next = new URLSearchParams(params)
    for (const k of ['connection', 'status', 'reason']) next.delete(k)
    setParams(next, { replace: true })
  }

  async function disconnect(c: ConnectionOut) {
    setBusy(c.id)
    setError('')
    try {
      await disconnectMine(c.id)
      load()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not disconnect.')
    } finally {
      setBusy(null)
    }
  }

  if (data === null) return error ? <p className="text-[13px] text-destructive">{error}</p> : null

  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-4" data-testid="connections-panel">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Connections</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Accounts canopy holds for you, so your tools can act as you without a token on your laptop.
        </p>
      </div>
      <ConnectOutcome params={outcome} onDismiss={dismiss} knownProviders={data.providers} />
      {error && <p className="text-[13px] text-destructive">{error}</p>}
      {data.providers.map((p) => (
        <ProviderRow
          key={`${p.name}:${p.workspace ?? ''}`}
          provider={p}
          rows={connectionsUnder(p, data.providers, data.connections)}
          connectHref={connectionStartPath(p.name, p.workspace ?? '', pathname)}
          busy={busy}
          onDisconnect={disconnect}
        />
      ))}
    </div>
  )
}

/** What a callback `reason` code says, as a fixed sentence (never the query text). */
const REASONS: Record<string, string> = {
  not_configured: 'is not set up on this deployment.',
  access_denied: 'was not approved on the provider’s consent screen.',
  bad_state: 'took too long or was started elsewhere. Try again.',
  forbidden: 'needs an administrator of the agent.',
  insufficient_scope: 'was connected without every permission canopy needs. Connect again and allow them all.',
}

/** The `?connection=…&status=…&reason=…` result banner, shared with the agent panel.
 *  The query string is attacker-controllable, so nothing from it is shown
 *  verbatim: the provider only if it is a known one, the reason only as a fixed string. */
export function ConnectOutcome({
  params,
  onDismiss,
  knownProviders,
}: {
  params: URLSearchParams | null
  onDismiss: () => void
  knownProviders: readonly { name: string; title: string }[]
}) {
  const key = params?.get('connection')
  if (!params || !key) return null
  const known = knownProviders.find((p) => p.name === key)
  const who = known ? known.title : 'The connection'
  const ok = params.get('status') === 'connected'
  const reason = REASONS[params.get('reason') ?? '']
  const text = ok
    ? `${who} connected.`
    : reason
      ? `${who} ${reason}`
      : `Connecting ${known ? known.title : 'the service'} failed. Try again.`
  return (
    <div
      role="status"
      className={`flex items-start justify-between gap-3 rounded border p-3 text-[13px] ${
        ok ? 'border-success/30 bg-success/10 text-success' : 'border-destructive/30 bg-destructive/10 text-destructive'
      }`}
    >
      <p>{text}</p>
      <button type="button" aria-label="Dismiss" onClick={onDismiss} className="shrink-0 opacity-70 hover:opacity-100">
        ×
      </button>
    </div>
  )
}

export function ConnectLink({ label, href }: { label: string; href: string }) {
  return (
    <a
      href={href}
      className="inline-flex items-center rounded bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90"
    >
      {label}
    </a>
  )
}

/** What the grant allows at the provider, shared with the agent panel. */
export function Scopes({ c }: { c: ConnectionOut }) {
  const scopes = c.scopes ?? []
  if (scopes.length === 0) return null
  return <p className="text-[12px] text-muted-foreground">Scopes: {scopes.join(', ')}</p>
}

export function lastUsed(c: ConnectionOut): string {
  return c.last_used_at ? `Last used ${new Date(c.last_used_at).toLocaleDateString()}` : 'Not used yet'
}

/** The row's second line: the account's API host when the provider has one per
 *  account (Salesforce's instance URL; a fixed-host provider sends none), then use. */
export function detailLine(c: ConnectionOut): string {
  return [c.instance_url, lastUsed(c)].filter(Boolean).join(' · ')
}

function ProviderRow({
  provider,
  rows,
  connectHref,
  busy,
  onDisconnect,
}: {
  provider: ProviderOut
  rows: ConnectionOut[]
  connectHref: string
  busy: number | null
  onDisconnect: (c: ConnectionOut) => void
}) {
  return (
    <div className="space-y-2" data-testid={`connection-provider-${provider.name}`}>
      <p className="text-[11px] uppercase tracking-wider font-semibold text-muted-foreground">{provider.title}</p>
      {rows.map((c) => (
        <div key={c.id} className="flex items-center justify-between gap-4 rounded border border-border bg-muted/40 p-2.5">
          <div className="text-[13px]">
            <p className="font-medium text-foreground">{c.account_label}</p>
            <p className="text-[12px] text-muted-foreground">{detailLine(c)}</p>
            <Scopes c={c} />
            {c.needs_reconnect && (
              <p className="text-[12px] text-warning">This connection expired or was revoked. Reconnect it.</p>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {c.needs_reconnect && <ConnectLink label="Reconnect" href={connectHref} />}
            <Button size="sm" variant="outline" disabled={busy === c.id} onClick={() => onDisconnect(c)}>
              {busy === c.id ? 'Disconnecting…' : 'Disconnect'}
            </Button>
          </div>
        </div>
      ))}
      {!provider.configured ? (
        <div>
          <Button size="sm" disabled>Connect {provider.title}</Button>
          <p className="mt-1 text-[12px] text-muted-foreground">Not set up on this deployment</p>
        </div>
      ) : rows.length === 0 ? (
        <ConnectLink label={`Connect ${provider.title}`} href={connectHref} />
      ) : null}
    </div>
  )
}
