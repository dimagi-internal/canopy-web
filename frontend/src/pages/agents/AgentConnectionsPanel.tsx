import { Fragment, useCallback, useEffect, useState } from 'react'
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
import { AccountRow } from '@/pages/agents/AccountRow'
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
 * One row per connection (and one per provider with nothing connected) in the
 * accounts list, using AccountRow; nothing provider-specific here except that
 * Google folds into the mailbox row (see ConnectionRows).
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
type Agent = { slug: string; name?: string; workspace?: string | null }

/** Everything the connection rows and their banner share: one fetch, one place for the
 *  busy/notice/error state. Pass `enabled: false` for someone who may not see them. */
export function useAgentConnections(agent: Agent, enabled = true) {
  const [data, setData] = useState<ConnectionsView | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [notice, setNotice] = useState('')
  const { pathname } = useLocation()
  const [params, setParams] = useSearchParams()
  const outcome = params.get('connection') ? params : null
  // The agent's workspace: the API names it on every provider row.
  const workspace = data?.providers.find((p) => p.workspace)?.workspace || agent.workspace || ''

  const load = useCallback(() => {
    if (!enabled) return
    listAgentConnections(agent.slug).then((d) => { setData(d); setError('') }).catch((e) => {
      // Its admins' alone, whatever the workspace role: for anyone else there is nothing to show.
      if (e instanceof ApiError && (e.status === 403 || e.status === 404)) setError('')
      else setError('Could not read the agent’s connections.')
    })
  }, [agent.slug, enabled])
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
  return { data, error, busy, notice, pathname, outcome, workspace, load, dismiss, run }
}
export type AgentConnections = ReturnType<typeof useAgentConnections>

/** The result banner after a sign-in, and any error or notice: sits at the top of the tab. */
export function ConnectionsBanner({ conns }: { conns: AgentConnections }) {
  const { data, error, notice, outcome, dismiss } = conns
  return (
    <>
      {data && <ConnectOutcome params={outcome} onDismiss={dismiss} knownProviders={data.providers} />}
      {error && <p className="mb-3 text-[13px] text-destructive">{error}</p>}
      {notice && <p role="status" className="mb-3 text-[13px] text-success">{notice}</p>}
    </>
  )
}

/** Google is two paths today: the agent's mailbox sign-in, and a Connections google
 *  provider. They fold together later (2026-10-31 spec, "Existing Google paths"). Until
 *  then the list shows ONE Google row: the mailbox row when the agent has a mailbox, else
 *  this provider's. When the mailbox row shows, the provider is hidden unless the agent
 *  already has a Connections google connection, which must stay visible so it can be
 *  disconnected: that one reads "Google (other account)". */
const MAILBOX_GOOGLE = 'google'

function rank(name: string): number {
  return name === 'google' ? 0 : name === 'salesforce' ? 1 : 2
}

export function ConnectionRows({ agent, conns, mailboxShown }: { agent: Agent; conns: AgentConnections; mailboxShown: boolean }) {
  const { data, busy, pathname, workspace, load, run } = conns
  const email = useAuth().user?.email ?? ''
  const canSetUp = roleAllows(useWorkspaceRole(workspace), 'integrations')
  if (data === null) return null
  const providers = [...data.providers].sort(
    (a, b) => rank(a.name) - rank(b.name) || a.title.localeCompare(b.title),
  )
  return (
    <>
      {providers.map((p) => {
        const rows = listed(data.connections).filter((c) => c.provider === p.name)
        const folded = p.name === MAILBOX_GOOGLE && mailboxShown
        if (folded && rows.length === 0) return null
        const title = folded ? 'Google (other account)' : p.title
        // A connection left behind by a move does not count: connect again here.
        const own = rows.filter((c) => !c.granted_from && c.usable)
        const start = { action: connectionStartAction(p.name, p.workspace || workspace), returnTo: pathname, agent: agent.slug }
        return (
          <Fragment key={p.name}>
            {rows.map((c) => (
              <ConnectionRow
                key={c.id}
                title={title}
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
            {folded ? null : !p.configured ? (
              <AccountRow
                testId={`agent-connection-provider-${p.name}`}
                name={title}
                pill={{ label: 'Not set up', tone: 'neutral' }}
                detail="Not set up for this workspace"
                action={
                  canSetUp && workspace ? (
                    <Link to={scopedPath(workspace, '/settings/connections')} className="text-[12px] text-primary hover:underline">
                      Set it up
                    </Link>
                  ) : null
                }
              />
            ) : own.length === 0 ? (
              <AccountRow
                testId={`agent-connection-provider-${p.name}`}
                name={title}
                pill={{ label: 'Not connected', tone: 'neutral' }}
                action={<ConnectForm label={`Connect ${p.title}`} primary {...start} />}
              />
            ) : null}
          </Fragment>
        )
      })}
    </>
  )
}

interface StartForm {
  action: string
  returnTo: string
  agent: string
}

/** Starts an agent's connect: a full-page form POST carrying the CSRF token. */
function ConnectForm({ label, action, returnTo, agent, primary = false }: { label: string; primary?: boolean } & StartForm) {
  return (
    <form method="post" action={action} className="inline-block">
      <input type="hidden" name="csrfmiddlewaretoken" value={getCsrfToken()} />
      <input type="hidden" name="return_to" value={returnTo} />
      <input type="hidden" name="agent" value={agent} />
      <Button type="submit" size="sm" variant={primary ? 'default' : 'outline'}>{label}</Button>
    </form>
  )
}

function ConnectionRow({
  title, c, start, busy, returnTo, canChange, agent, onDisconnect, onChanged,
}: {
  title: string
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
  const needsReconnect = c.usable && (c.needs_reconnect || missing !== '')
  const pill: { label: string; tone: 'success' | 'warning' } = !c.usable
    ? { label: 'Not working', tone: 'warning' }
    : needsReconnect
      ? { label: 'Reconnect needed', tone: 'warning' }
      : lent && c.state === 'unshared'
        ? { label: 'Not shared yet', tone: 'warning' }
        : !lent && c.state === 'unshared'
          ? { label: 'Not ready', tone: 'warning' }
          : { label: 'Connected', tone: 'success' }
  return (
    <AccountRow
      testId={`agent-connection-${c.id}`}
      name={title}
      pill={pill}
      detail={c.account_label}
      action={
        !lent ? (
          <>
            {needsReconnect && <ConnectForm label="Reconnect" {...start} />}
            <Button size="sm" variant="outline" disabled={busy} onClick={onDisconnect}>
              {busy ? 'Working…' : 'Disconnect'}
            </Button>
          </>
        ) : null
      }
    >
      <p>
        {detailLine(c)}
        {lent && <> · Lent by {c.granted_from}</>}
      </p>
      <Scopes c={c} />
      {!c.usable ? (
        <p className="text-warning">
          {lent
            ? `${c.granted_from} is no longer in ${where}, where this was made, so it no longer works.`
            : `Made in ${where} before the move — disconnect it and connect here.`}
        </p>
      ) : lent ? (
        c.needs_reconnect ? <p className="text-warning">Reconnect required: its owner must reconnect it.</p>
          : c.state === 'unshared' ? <p className="text-warning">Not shared yet: its owner must share it again before this agent can use it.</p>
          : missing ? <p className="text-warning">Its owner must reconnect to allow: {missing}</p> : null
      ) : (
        <>
          <SharingStatus c={c} returnTo={returnTo} canChange={canChange} onChanged={onChanged} owner={agent} />
          {!c.needs_reconnect && missing && <p className="text-warning">Reconnect to allow: {missing}</p>}
        </>
      )}
      {!lent && c.usable && c.provider_calls && (
        <AgentScreenShares
          connectionId={c.id}
          agentName={agent.name || agent.slug}
          workspace={c.workspace ?? ''}
          serverUse={!!c.server_use}
        />
      )}
    </AccountRow>
  )
}
