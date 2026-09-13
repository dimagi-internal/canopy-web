import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  disconnectGitHub,
  getGitHubConnection,
  gitHubConnectPath,
  type GitHubConnectionOut,
} from '@/api/github'
import { Button } from 'canopy-ui/ui'

/**
 * Connect a GitHub account, so creating an agent can create its repo.
 *
 * Sits beside "Connect Claude Subscription" because it is the same shape of
 * thing: a per-user grant to a third party that canopy holds on your behalf.
 *
 * WHAT THE COPY IS FOR. The scope guidance is not decoration. The permission
 * this app requests (`Administration: write`, needed to create a repository at
 * all) sounds alarming, and the thing that makes it safe is a choice the user
 * makes on GitHub's own screen, not here: picking "Only select repositories"
 * means canopy reaches the repos it creates for you and nothing else, because
 * GitHub automatically grants an app access to repositories it created. Left
 * unsaid, most people will click "All repositories" — it reads like the
 * default — and grant far more than this needs. So it is said, at the moment
 * of choosing, rather than in a doc nobody opens.
 */
export function GitHubPanel() {
  const [conn, setConn] = useState<GitHubConnectionOut | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [params, setParams] = useSearchParams()

  // The callback comes back as a full-page load at /settings?github=… — the
  // OAuth flow leaves the SPA, so this is how the result gets reported.
  const outcome = params.get('github')
  const outcomeDetail = params.get('detail') ?? ''

  useEffect(() => {
    getGitHubConnection()
      .then(setConn)
      .catch(() => setError('Could not read the GitHub connection.'))
  }, [outcome])

  function dismissOutcome() {
    const next = new URLSearchParams(params)
    next.delete('github')
    next.delete('detail')
    next.delete('login')
    setParams(next, { replace: true })
  }

  async function handleDisconnect() {
    setBusy(true)
    setError('')
    try {
      await disconnectGitHub()
      setConn(await getGitHubConnection())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not disconnect GitHub.')
    } finally {
      setBusy(false)
    }
  }

  if (conn === null) return null

  // Nothing to offer and no button that could work — say so rather than
  // rendering a Connect that dead-ends. This is the state of a fresh checkout
  // and of any deployment where the client secret has not been set yet.
  if (!conn.configured) {
    return (
      <div className="rounded-xl border border-border bg-card p-5 space-y-2">
        <h2 className="text-sm font-semibold text-foreground">GitHub</h2>
        <p className="text-sm text-muted-foreground">
          GitHub is not set up on this deployment, so creating an agent cannot create its
          repository yet. An administrator needs to configure the GitHub App credentials.
        </p>
      </div>
    )
  }

  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-4">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Connect GitHub</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Creating an agent creates a real repository for it. Connect GitHub once and canopy
          can do that on your behalf.
        </p>
      </div>

      {outcome === 'connected' && (
        <StatusNote tone="success" onDismiss={dismissOutcome}>
          GitHub connected.
        </StatusNote>
      )}
      {outcome === 'installation_updated' && (
        <StatusNote tone="info" onDismiss={dismissOutcome}>
          Your GitHub installation was updated. The owner list on the create-agent form will
          pick it up.
        </StatusNote>
      )}
      {outcome === 'error' && (
        <StatusNote tone="error" onDismiss={dismissOutcome}>
          {outcomeDetail || 'Connecting GitHub failed. Try again.'}
        </StatusNote>
      )}

      {error && <p className="text-[13px] text-destructive">{error}</p>}

      {conn.connected && !conn.needs_reconnect ? (
        <div className="flex items-center justify-between gap-4">
          <p className="text-[13px] text-foreground-secondary">
            Connected as{' '}
            <span className="font-medium text-foreground">@{conn.github_login}</span>
          </p>
          <Button size="sm" variant="outline" onClick={handleDisconnect} disabled={busy}>
            {busy ? 'Disconnecting…' : 'Disconnect'}
          </Button>
        </div>
      ) : (
        <div className="space-y-3">
          {conn.connected && conn.needs_reconnect && (
            <p className="text-[13px] text-warning">
              Your GitHub connection expired or was revoked. Reconnect it to create agents.
            </p>
          )}
          <p className="text-[13px] text-muted-foreground">
            On GitHub&apos;s screen, choose{' '}
            <span className="font-medium text-foreground">Only select repositories</span>.
            Canopy is automatically given access to the agent repositories it creates for you,
            so it does not need access to anything you already have.
          </p>
          {/* A real anchor, not a Button with onClick: this is a full-page
              navigation out of the SPA (GitHub has to show its own consent
              screen), and a link keeps middle-click and "copy link" working. */}
          <a
            href={gitHubConnectPath()}
            className="inline-flex items-center rounded bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90"
          >
            {conn.connected ? 'Reconnect GitHub' : 'Connect GitHub'}
          </a>
        </div>
      )}

      {conn.connected && conn.install_url && (
        <p className="text-[12px] text-muted-foreground">
          Need canopy to create repos in an organisation?{' '}
          <a
            href={conn.install_url}
            target="_blank"
            rel="noreferrer"
            className="text-primary hover:underline"
          >
            Install it there
          </a>
          , then it appears in the owner list when you create an agent.
        </p>
      )}

      {conn.connected && (
        <p className="text-[12px] text-foreground-subtle">
          Disconnecting removes canopy&apos;s copy of the grant. To revoke it on GitHub&apos;s
          side as well, use{' '}
          <a
            href="https://github.com/settings/applications"
            target="_blank"
            rel="noreferrer"
            className="text-primary hover:underline"
          >
            your GitHub authorized apps
          </a>
          .
        </p>
      )}
    </div>
  )
}

function StatusNote({
  tone,
  children,
  onDismiss,
}: {
  tone: 'success' | 'info' | 'error'
  children: React.ReactNode
  onDismiss: () => void
}) {
  const toneClass =
    tone === 'success'
      ? 'border-success/30 bg-success/10 text-success'
      : tone === 'error'
        ? 'border-destructive/30 bg-destructive/10 text-destructive'
        : 'border-info/30 bg-info/10 text-info'
  return (
    <div className={`flex items-start justify-between gap-3 rounded border p-3 ${toneClass}`}>
      <p className="text-[13px]">{children}</p>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss"
        className="shrink-0 text-[13px] opacity-70 hover:opacity-100"
      >
        ×
      </button>
    </div>
  )
}
