import { useCallback, useEffect, useState } from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'
import {
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
 *
 * A provider is set up per WORKSPACE (its admin registers the OAuth app), so the
 * API lists one provider row per (workspace, provider). They render as one
 * section per provider with a Connect button per workspace; the workspace is
 * named only where leaving it out would be ambiguous.
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
      {groupByProvider(data.providers).map((rows) => (
        <ProviderSection
          key={rows[0].name}
          rows={rows}
          connections={data.connections.filter((c) => c.provider === rows[0].name)}
          nameWorkspaces={new Set(data.connections.map((c) => c.workspace ?? '')).size > 1}
          startPath={(ws) => connectionStartPath(rows[0].name, ws, pathname)}
          busy={busy}
          onDisconnect={disconnect}
        />
      ))}
    </div>
  )
}

/** What a callback `reason` code says, as a fixed sentence (never the query text). */
const REASONS: Record<string, string> = {
  not_configured: 'is not set up for this workspace.',
  access_denied: 'was not approved on the provider’s consent screen.',
  bad_state: 'took too long or was started elsewhere. Try again.',
  // Also what someone sees after leaving the workspace, or for an agent that moved:
  // not only a missing admin role.
  forbidden: 'is no longer allowed for this account or workspace.',
  insufficient_scope: 'was connected without every permission canopy needs. Connect again and allow them all.',
  wrong_account:
    'was signed in with an account this connection may not use — sign in as the agent’s own account, or one in the workspace’s allowed domains.',
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

/** Scopes the app now asks for that this grant lacks (an admin added one after it
 *  was connected), as the words for what each allows; "" when none are missing. */
export function missingScopes(c: ConnectionOut): string {
  return (c.missing_scopes ?? []).map((s) => s.label || s.scope).join(', ')
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

/** The provider rows, grouped by provider, in the order the API listed them. */
function groupByProvider(providers: readonly ProviderOut[]): ProviderOut[][] {
  const groups = new Map<string, ProviderOut[]>()
  for (const p of providers) groups.set(p.name, [...(groups.get(p.name) ?? []), p])
  return [...groups.values()]
}

function workspaceLabel(r: { workspace?: string; workspace_name?: string }): string {
  return r.workspace_name || r.workspace || ''
}

function ProviderSection({
  rows,
  connections,
  nameWorkspaces,
  startPath,
  busy,
  onDisconnect,
}: {
  rows: ProviderOut[]
  connections: ConnectionOut[]
  /** The person has connections in more than one workspace: say which each is in. */
  nameWorkspaces: boolean
  startPath: (workspace: string) => string
  busy: number | null
  onDisconnect: (c: ConnectionOut) => void
}) {
  const { name, title } = rows[0]
  const configured = rows.filter((r) => r.configured)
  // A workspace already connected in shows its connection, not another Connect.
  const connectable = configured.filter((r) => !connections.some((c) => c.workspace === r.workspace))
  return (
    <div className="space-y-2" data-testid={`connection-provider-${name}`}>
      <p className="text-[11px] uppercase tracking-wider font-semibold text-muted-foreground">{title}</p>
      {connections.map((c) => {
        // A connection in a workspace the person has left hands out nothing (the CLI
        // says `not_connected`) and its connect page is gone: Disconnect only.
        const missing = c.usable ? missingScopes(c) : ''
        const reconnect = c.usable && (c.needs_reconnect || missing !== '')
        return (
          <div key={c.id} className="flex items-center justify-between gap-4 rounded border border-border bg-muted/40 p-2.5">
            <div className="text-[13px]">
              <p className="font-medium text-foreground">{c.account_label}</p>
              <p className="text-[12px] text-muted-foreground">
                {[nameWorkspaces && c.usable ? `In ${workspaceLabel(c)}` : '', detailLine(c)].filter(Boolean).join(' · ')}
              </p>
              <Scopes c={c} />
              {!c.usable ? (
                <p className="text-[12px] text-warning">
                  You are no longer in {workspaceLabel(c)}, so this connection no longer works. Disconnect it.
                </p>
              ) : c.needs_reconnect ? (
                <p className="text-[12px] text-warning">This connection expired or was revoked. Reconnect it.</p>
              ) : missing ? (
                <p className="text-[12px] text-warning">Reconnect to allow: {missing}</p>
              ) : null}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              {reconnect && <ConnectLink label="Reconnect" href={startPath(c.workspace ?? '')} />}
              <Button size="sm" variant="outline" disabled={busy === c.id} onClick={() => onDisconnect(c)}>
                {busy === c.id ? 'Disconnecting…' : 'Disconnect'}
              </Button>
            </div>
          </div>
        )
      })}
      {configured.length === 0 ? (
        <div>
          <Button size="sm" disabled>Connect {title}</Button>
          <p className="mt-1 text-[12px] text-muted-foreground">Not set up in your workspaces</p>
        </div>
      ) : connectable.length > 0 ? (
        <div className="flex flex-wrap gap-2">
          {connectable.map((r) => (
            <ConnectLink
              key={r.workspace}
              label={configured.length > 1 ? `Connect ${title} in ${workspaceLabel(r)}` : `Connect ${title}`}
              href={startPath(r.workspace ?? '')}
            />
          ))}
        </div>
      ) : null}
    </div>
  )
}
