import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Button } from 'canopy-ui/ui'

import {
  getAgentCredentialStatus,
  getGoogleMint,
  startGoogleMint,
  type AgentCredentialStatusOut,
  type GoogleMintOut,
} from '@/api/agents'
import { getAgentMailbox, type AgentMailboxOut } from '@/api/mail'
import { AccountRow } from '@/pages/agents/AccountRow'
import { ConnectionRows, ConnectionsBanner, useAgentConnections } from '@/pages/agents/AgentConnectionsPanel'
import { AgentGitHubSection } from '@/pages/agents/AgentGitHubSection'
import { declaresMailbox, followMint, GOG_TOKEN_REF, mintOutcome } from '@/pages/agents/googleMint'

// The agent's External accounts tab: the outside accounts it acts as, as ONE list,
// one row per account, every row alike: service name | status and one short detail |
// one action. GitHub first, then Google, Salesforce, and any other Connections
// provider. Rows from Connections are admin-only (the server enforces it too).

export function AgentAccountsPanel({
  agent,
  isAdmin = false,
}: {
  agent: { slug: string; email?: string | null; name?: string; workspace?: string | null }
  isAdmin?: boolean
}) {
  const [rows, setRows] = useState<AgentCredentialStatusOut[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [params] = useSearchParams()
  // The sign-in finishes on one of the agent's runners after Google sends the person
  // back (`?google=pending&mint=…`), so the return trip FOLLOWS it to its end.
  const returnedMint = params.get('mint')
  const [finished, setFinished] = useState<GoogleMintOut | null>(null)
  const returned = finished
    ? { status: finished.outcome || (finished.status === 'done' ? 'ok' : 'expired'), account: finished.account,
        expected: finished.expected_account }
    : { status: params.get('google'), account: params.get('account'), expected: params.get('expected') }
  const outcome = mintOutcome(returned.status, { account: returned.account, agentEmail: returned.expected || agent.email })
  const [progress, setProgress] = useState<string | null>(null)
  const [mailBox, setMailBox] = useState<AgentMailboxOut | null>(null)
  const conns = useAgentConnections(agent, isAdmin)

  useEffect(() => {
    getAgentCredentialStatus(agent.slug).then(setRows, () => setRows([]))
  }, [agent.slug])
  useEffect(() => {
    if (!agent.email?.trim()) return
    getAgentMailbox(agent.slug).then(setMailBox).catch(() => undefined)
  }, [agent.slug, agent.email])

  useEffect(() => {
    if (!returnedMint || params.get('google') !== 'pending') return
    let live = true
    void (async () => {
      const end = await followMint(() => getGoogleMint(agent.slug, returnedMint), (m) => m.status === 'done' || m.status === 'failed')
      if (!live) return
      if (end) {
        setFinished(end)
        if (end.status === 'done') getAgentCredentialStatus(agent.slug).then(setRows, () => undefined)
      } else {
        setError('The runner has not finished the sign-in yet — reload this page in a minute to see how it ended.')
      }
    })()
    return () => {
      live = false
    }
  }, [agent.slug, returnedMint, params])

  const connectMailbox = async () => {
    setBusy(true)
    setError(null)
    try {
      const mint = await startGoogleMint(agent.slug)
      setProgress(`Asking ${mint.runner || 'the agent’s runner'} to prepare the Google sign-in…`)
      const ready = await followMint(() => getGoogleMint(agent.slug, mint.id), (m) => Boolean(m.authorize_url) || m.status === 'failed')
      if (!ready || !ready.authorize_url) {
        setProgress(null)
        setError(ready?.detail || `${mint.runner || 'The runner'} did not prepare the sign-in in time — it may be busy or offline. Try again.`)
        setBusy(false)
        return
      }
      // A TOP-LEVEL navigation, deliberately. Following the redirect inside
      // fetch() lands on Google's HTML with nothing shown to the user; only
      // leaving the page can render a consent screen.
      window.location.assign(ready.authorize_url)
    } catch (e) {
      setProgress(null)
      setError(e instanceof Error ? e.message : 'Could not start the Google sign-in')
      setBusy(false)
    }
  }

  // A canopy-hosted address has no Google mailbox to connect (apps/mail): it must not
  // imply a gog-token, nor read "Not connected" for something that needs nothing.
  const hosted = mailBox?.kind === 'hosted'
  const hasMailbox = rows !== null && declaresMailbox(rows, hosted ? null : agent.email)
  const gog = rows?.find((r) => r.name === GOG_TOKEN_REF)
  const connected = Boolean(gog?.set)

  return (
    <div data-testid="agent-accounts">
      {outcome && (
        <p
          className={`mb-3 text-[13px] ${outcome.tone === 'ok' ? 'text-success' : 'text-destructive'}`}
          data-testid="google-mint-outcome"
        >
          {outcome.text}
          {outcome.link && (
            <>
              {' '}
              <Link to={outcome.link.href} className="font-medium text-primary hover:underline">
                {outcome.link.label}
              </Link>
            </>
          )}
        </p>
      )}
      <ConnectionsBanner conns={conns} />

      <div role="list" className="divide-y divide-border" data-testid="credential-status">
        <AgentGitHubSection slug={agent.slug} />
        {hasMailbox && (
          // Signing in is the only way a mailbox token is minted, so the action sits on its row.
          <AccountRow
            id="cred-mailbox"
            testId="credential-status-mailbox"
            name="Google"
            pill={connected ? { label: 'Connected', tone: 'success' } : { label: 'Not connected', tone: 'neutral' }}
            detail={hosted ? null : agent.email}
            action={
              <Button size="sm" variant={connected ? 'outline' : 'default'} disabled={busy} onClick={() => void connectMailbox()}>
                {connected ? 'Reconnect' : 'Connect Google'}
              </Button>
            }
          >
            {progress && !error && <p data-testid="google-mint-progress">{progress}</p>}
            {error && <p className="text-destructive">{error}</p>}
          </AccountRow>
        )}
        {isAdmin && rows !== null && <ConnectionRows agent={agent} conns={conns} mailboxShown={hasMailbox} />}
        {hosted && (
          <AccountRow
            testId="credential-status-email"
            name="Email"
            pill={mailBox?.live ? { label: 'Canopy-hosted', tone: 'success' } : { label: 'Canopy-hosted, not routed', tone: 'warning' }}
            action={<a href="#reach" className="text-[12px] text-primary hover:underline">Channels</a>}
          />
        )}
      </div>
      {!hasMailbox && error && <p className="mt-3 text-[13px] text-destructive">{error}</p>}
    </div>
  )
}
