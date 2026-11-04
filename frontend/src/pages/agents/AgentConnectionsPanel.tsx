import { useCallback, useEffect, useState } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import {
  connectionStartAction,
  disconnectAgentConnection,
  listAgentConnections,
  type ConnectionOut,
  type ConnectionsView,
} from '@/api/connections'
import { getCsrfToken } from '@/api/base'
import { ApiError } from '@/api/problem'
import { ConnectOutcome, Scopes, SharingStatus, detailLine, disconnected, listed, missingScopes } from '@/components/settings/ConnectionsPanel'
import { Button } from 'canopy-ui/ui'
import { roleAllows } from '@/lib/workspaceRoles'
import { scopedPath } from '@/lib/scopedLinks'
import { useAuth } from '@/auth/AuthProvider'
import { AgentScreenShares } from './AgentScreenShares'
import { useWorkspaceRole } from '@/workspace/WorkspaceProvider'

/**
 * What an agent can act as through connections: its own, and ones lent to it by
 * another agent or a person. Admin-only (the server enforces it too).
 *
 * Disconnecting asks for a fresh passkey; the API client's step-up middleware
 * prompts and retries, so this just calls DELETE.
 *
 * One section per provider the API lists; nothing provider-specific here.
 *
 * Connecting is a form POST (CSRF-checked), never a link: the server refuses a
 * GET that names an agent, and asks the provider for a fresh login where it
 * supports one, so the account attached is the one the admin types in — the
 * agent's — not whichever session their browser happens to hold.
 *
 * An agent connects with its OWN workspace's app (its admin sets it up under
 * the workspace's Settings → Connections), so the start URL names that workspace.
 * A connection works only while its owning agent is in the workspace whose app
 * made it (`usable`): one made before the agent moved is shown as such, with
 * Disconnect only, and the Connect button returns for the agent's workspace. A
 * Reconnect goes to the connection's own workspace (`c.workspace`), never the
 * provider row's: after a move those differ, and the server refuses the other.
 */
export function AgentConnectionsPanel({ agent }: { agent: { slug: string; name?: string; workspace?: string | null } }) {
  const [data, setData] = useState<ConnectionsView | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [notice, setNotice] = useState('')
  const { pathname } = useLocation()
  const [params, setParams] = useSearchParams()
  const outcome = params.get('connection') ? params : null
  // The agent's workspace: the API names it on every provider row.
  const workspace = data?.providers.find((p) => p.workspace)?.workspace || agent.workspace || ''
  const email = useAuth().user?.email ?? ''
  const canSetUp = roleAllows(useWorkspaceRole(workspace), 'integrations')

  const load = useCallback(() => {
    listAgentConnections(agent.slug).then((d) => { setData(d); setError('') }).catch((e) => {
      // Its admins' alone, whatever the workspace role: for anyone else there is nothing to show.
      if (e instanceof ApiError && (e.status === 403 || e.status === 404)) setError('')
      else setError('Could not read the agent’s connections.')
    })
  }, [agent.slug])
  useEffect(load, [load, outcome?.get('status')])
  function dismiss() {
    const next = new URLSearchParams(params)
    for (const k of ['connection', 'status', 'reason']) next.delete(k)
    setParams(next, { replace: true })
  }

  async function run(id: string, what: () => Promise<string>) {
    setBusy(id)
    setError('')
    setNotice('')
    try {
      setNotice(await what())
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
        const rows = listed(data.connections).filter((c) => c.provider === p.name)
        // A connection left behind by a move does not count: connect again here.
        const own = rows.filter((c) => !c.granted_from && c.usable)
        const start = { action: connectionStartAction(p.name, p.workspace || workspace), returnTo: pathname, agent: agent.slug }
        return (
          <div key={p.name} className="space-y-2" data-testid={`agent-connection-provider-${p.name}`}>
            <p className="text-[11px] uppercase tracking-wider font-semibold text-muted-foreground">{p.title}</p>
            {rows.map((c) => (
              <Row
                key={c.id}
                c={c}
                start={{ ...start, action: connectionStartAction(c.provider, c.workspace ?? '') }}
                busy={busy === c.id}
                returnTo={pathname}
                agent={agent}
                canChange={!!email && !!c.connected_by_email && c.connected_by_email.toLowerCase() === email.toLowerCase()}
                onChanged={load}
                onDisconnect={() => run(c.id, async () => disconnected(await disconnectAgentConnection(agent.slug, c.id), c.owner_key_replaced))}
              />
            ))}
            {!p.configured ? (
              <div>
                <Button size="sm" disabled>Connect {p.title}</Button>
                <p className="mt-1 text-[12px] text-muted-foreground">
                  Not set up for this workspace
                  {canSetUp && workspace && (
                    <>
                      {' · '}
                      <Link to={scopedPath(workspace, '/settings/connections')} className="text-primary hover:underline">
                        Set it up
                      </Link>
                    </>
                  )}
                </p>
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
  c, start, busy, returnTo, canChange, agent, onDisconnect, onChanged,
}: {
  agent: { slug: string; name?: string }
  c: ConnectionOut
  start: StartForm
  busy: boolean
  returnTo: string
  canChange: boolean
  onDisconnect: () => void
  onChanged: () => void
}) {
  // One lent to this agent: its owner changes or withdraws it by sharing again, not here.
  const lent = !!c.granted_from
  const where = c.workspace_name || c.workspace
  const missing = c.usable ? missingScopes(c) : ''
  return (
    <div className="space-y-2 rounded border border-border bg-muted/40 p-2.5" data-testid={`agent-connection-${c.id}`}>
      <div className="flex items-center justify-between gap-4">
        <div className="text-[13px]">
          <p className="font-medium text-foreground">{c.account_label}</p>
          <p className="text-[12px] text-muted-foreground">
            {detailLine(c)}
            {lent && <> · Lent by {c.granted_from}</>}
          </p>
          <Scopes c={c} />
          {!c.usable ? (
            <p className="text-[12px] text-warning">
              {lent
                ? `${c.granted_from} is no longer in ${where}, where this was made, so it no longer works.`
                : `Made in ${where} before the move — disconnect it and connect here.`}
            </p>
          ) : lent ? (
            c.needs_reconnect ? <p className="text-[12px] text-warning">Reconnect required: its owner must reconnect it.</p>
              : c.state === 'unshared' ? <p className="text-[12px] text-warning">Not shared yet: its owner must share it again before this agent can use it.</p>
              : missing ? <p className="text-[12px] text-warning">Its owner must reconnect to allow: {missing}</p> : null
          ) : (
            <>
              <SharingStatus c={c} returnTo={returnTo} canChange={canChange} onChanged={onChanged} />
              {!c.needs_reconnect && missing && <p className="text-[12px] text-warning">Reconnect to allow: {missing}</p>}
            </>
          )}
        </div>
        {!lent && (
          <div className="flex shrink-0 items-center gap-2">
            {c.usable && (c.needs_reconnect || missing !== '') && <ConnectForm label="Reconnect" {...start} />}
            <Button size="sm" variant="outline" disabled={busy} onClick={onDisconnect}>
              {busy ? 'Working…' : 'Disconnect'}
            </Button>
          </div>
        )}
      </div>
      {!lent && c.usable && c.provider_calls && (
        <AgentScreenShares
          connectionId={c.id}
          agentName={agent.name || agent.slug}
          workspace={c.workspace ?? ''}
          serverUse={!!c.server_use}
        />
      )}
    </div>
  )
}
