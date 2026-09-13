import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  disconnectGitHub,
  getGitHubConnection,
  gitHubConnectPath,
  listGitHubInstallations,
  type GitHubConnectionOut,
  type GitHubInstallationOut,
} from '@/api/github'
import { Button } from 'canopy-ui/ui'

/**
 * Connect GitHub, so an agent's repository can be pushed to on your behalf.
 *
 * Sits beside "Connect Claude Subscription" because it is the same shape of
 * thing: a per-user grant to a third party that canopy holds for you.
 *
 * AUTHORIZING IS NOT THE SAME AS INSTALLING, and this panel's whole structure
 * exists because of that. GitHub has two separate consent flows:
 *
 *  - authorize — "may this app act as you?" Identity and a token. No
 *    repository picker.
 *  - install — "which account, and which repositories?" This is where access
 *    is actually granted, and GitHub owns that screen: we cannot render it, and
 *    should not be able to, since a third party drawing "which repos do you
 *    grant?" is a phishing surface.
 *
 * The first version of this panel sent people to authorize and then treated
 * `connected: true` as success. Measured on labs 2026-09-13: a user authorized
 * cleanly, the panel said "Connected as @jjackson", and they had access to ZERO
 * repositories — so nothing could be pushed and the UI said everything was
 * fine. Connect now starts the INSTALL flow (which authorizes in the same
 * trip), and "connected" is no longer treated as sufficient: the installation
 * list is fetched and an empty one is reported as unfinished setup.
 */
export function GitHubPanel() {
  const [conn, setConn] = useState<GitHubConnectionOut | null>(null)
  // `null` = not looked up yet. An empty ARRAY is a real, meaningful answer
  // (authorized but granted nothing), so the two cannot share a representation.
  const [installs, setInstalls] = useState<GitHubInstallationOut[] | null>(null)
  const [installsError, setInstallsError] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [params, setParams] = useSearchParams()

  // The callback returns as a full-page load at /settings?github=… — the OAuth
  // flow leaves the SPA, so this is the only way the result reaches us.
  const outcome = params.get('github')
  const outcomeDetail = params.get('detail') ?? ''

  useEffect(() => {
    let cancelled = false
    getGitHubConnection()
      .then((c) => {
        if (cancelled) return
        setConn(c)
        // Only meaningful when there is a usable grant to ask with. This is
        // the one fact the status endpoint cannot report — it never calls
        // GitHub, deliberately, so that /settings does not become slow or
        // fail because of a third party.
        if (!c.connected || c.needs_reconnect) {
          setInstalls(null)
          return
        }
        // An EMPTY result is re-checked once before it is believed. GitHub
        // does not list a brand-new installation immediately, so the first
        // read straight after coming back from the install screen can be
        // empty for a moment — and showing "no repository access" on that is
        // alarming and wrong. Observed on labs 2026-09-13: the warning
        // appeared and then vanished on its own.
        const read = (attempt: number): void => {
          listGitHubInstallations()
            .then((rows) => {
              if (cancelled) return
              if (rows.length === 0 && attempt === 0) {
                window.setTimeout(() => { if (!cancelled) read(1) }, 1500)
                return
              }
              setInstalls(rows)
            })
            .catch((e) => {
              if (cancelled) return
              setInstalls(null)
              setInstallsError(
                e instanceof Error ? e.message : 'Could not check your GitHub installations.',
              )
            })
        }
        read(0)
      })
      .catch(() => { if (!cancelled) setError('Could not read the GitHub connection.') })
    return () => { cancelled = true }
  }, [outcome])

  function dismissOutcome() {
    const next = new URLSearchParams(params)
    for (const k of ['github', 'detail', 'login']) next.delete(k)
    setParams(next, { replace: true })
  }

  async function handleDisconnect() {
    setBusy(true)
    setError('')
    try {
      await disconnectGitHub()
      setConn(await getGitHubConnection())
      setInstalls(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not disconnect GitHub.')
    } finally {
      setBusy(false)
    }
  }

  if (conn === null) return null

  if (!conn.configured) {
    return (
      <Card>
        <h2 className="text-sm font-semibold text-foreground">GitHub</h2>
        <p className="text-sm text-muted-foreground">
          GitHub is not set up on this deployment, so canopy cannot push an agent&apos;s
          repository yet. An administrator needs to configure the GitHub App credentials.
        </p>
      </Card>
    )
  }

  const connected = conn.connected && !conn.needs_reconnect
  // Authorized, but granted access to nothing. Not an edge case: it is what you
  // get by authorizing without installing, and it looks identical to success
  // unless the panel says otherwise.
  const noRepoAccess = connected && installs !== null && installs.length === 0

  return (
    <Card>
      <div>
        <h2 className="text-sm font-semibold text-foreground">Connect GitHub</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          An agent lives in its own repository. Connect GitHub once and canopy can push to
          the repositories you choose, on your behalf.
        </p>
      </div>

      {outcome === 'connected' && (
        <Note tone="success" onDismiss={dismissOutcome}>GitHub connected.</Note>
      )}
      {outcome === 'installation_updated' && (
        <Note tone="info" onDismiss={dismissOutcome}>
          Your GitHub installation was updated.
        </Note>
      )}
      {outcome === 'install_without_code' && (
        <Note tone="error" onDismiss={dismissOutcome}>
          GitHub installed the app but sent back no authorization code. The app is missing
          &ldquo;Request user authorization (OAuth) during installation&rdquo; — an
          administrator needs to enable it in the GitHub App settings.
        </Note>
      )}
      {outcome === 'error' && (
        <Note tone="error" onDismiss={dismissOutcome}>
          {outcomeDetail || 'Connecting GitHub failed. Try again.'}
        </Note>
      )}

      {error && <p className="text-[13px] text-destructive">{error}</p>}

      {!connected ? (
        <div className="space-y-3">
          {conn.connected && conn.needs_reconnect && (
            <p className="text-[13px] text-warning">
              Your GitHub connection expired or was revoked. Reconnect it to keep your agents
              pushing.
            </p>
          )}
          <ScopeAdvice />
          <ConnectLink label={conn.connected ? 'Reconnect GitHub' : 'Connect GitHub'} />
        </div>
      ) : noRepoAccess ? (
        <div className="space-y-3 rounded border border-warning/30 bg-warning/10 p-3">
          <p className="text-[13px] font-medium text-warning">
            Nearly there — canopy has no repository access yet.
          </p>
          <p className="text-[13px] text-foreground-secondary">
            You&apos;re signed in as{' '}
            <span className="font-medium text-foreground">@{conn.github_login}</span>, but you
            haven&apos;t granted access to any repositories. Authorizing and installing are
            separate steps on GitHub, and only the install step asks about repositories.
          </p>
          <ConnectLink label="Choose repositories" />
        </div>
      ) : (
        <div className="space-y-3">
          <div className="flex items-center justify-between gap-4">
            <p className="text-[13px] text-foreground-secondary">
              Connected as{' '}
              <span className="font-medium text-foreground">@{conn.github_login}</span>
            </p>
            <Button size="sm" variant="outline" onClick={handleDisconnect} disabled={busy}>
              {busy ? 'Disconnecting…' : 'Disconnect'}
            </Button>
          </div>
          {installs && installs.length > 0 && (
            <div className="space-y-2">
              <p className="text-[11px] uppercase tracking-wider font-semibold text-muted-foreground">
                Repository access
              </p>
              {installs.map((i) => (
                <InstallationRow key={i.installation_id} install={i} />
              ))}
            </div>
          )}
          {installsError && (
            <p className="text-[13px] text-muted-foreground">{installsError}</p>
          )}
          {conn.install_url && (
            <p className="text-[12px] text-muted-foreground">
              Need canopy in another account or organisation?{' '}
              <a
                href={conn.install_url}
                target="_blank"
                rel="noreferrer"
                className="text-primary hover:underline"
              >
                Install it there
              </a>
              .
            </p>
          )}
        </div>
      )}

      {conn.connected && (
        <p className="text-[12px] text-foreground-subtle">
          Disconnecting removes canopy&apos;s copy of the grant. To revoke it on
          GitHub&apos;s side too, use{' '}
          <a
            href="https://github.com/settings/installations"
            target="_blank"
            rel="noreferrer"
            className="text-primary hover:underline"
          >
            your GitHub installations
          </a>
          .
        </p>
      )}
    </Card>
  )
}

/**
 * What one installation actually reaches.
 *
 * Naming the repositories rather than only the account is the point. Reporting
 * "access granted on dimagi-internal" for an installation scoped to a single
 * repo reads like the whole organisation — it OVERSTATES the grant to the
 * person reading it, which is the opposite of useful when the advice above was
 * "scope it narrowly". If canopy tells people to pick carefully, it has to show
 * what they picked.
 *
 * An "all repositories" grant is called out rather than listed: enumerating it
 * could be thousands of rows, and it is the one grant the advice exists to
 * steer people away from, so it should read as a choice worth revisiting.
 */
function InstallationRow({ install }: { install: GitHubInstallationOut }) {
  // `repositories` is optional on the wire (it has a server-side default), and
  // `grants_all_repositories` is a service-layer property rather than a
  // serialized field — so the "all" case is derived from the one thing GitHub
  // actually tells us.
  const repos = install.repositories ?? []
  const grantsAll = install.repository_selection === 'all'
  const more = install.repository_count - repos.length
  return (
    <div className="rounded border border-border bg-muted/40 p-2.5">
      <p className="text-[13px]">
        <span className="font-medium text-foreground">{install.account_login}</span>
        <span className="text-muted-foreground">
          {install.is_org ? ' (organisation)' : ' (your account)'}
        </span>
      </p>
      {grantsAll ? (
        <p className="mt-1 text-[12px] text-warning">
          All repositories — canopy can reach every repository this account can see. Narrow
          it if you did not mean that.
        </p>
      ) : repos.length > 0 ? (
        <ul className="mt-1 space-y-0.5">
          {repos.map((full) => (
            <li key={full} className="text-[12px] text-foreground-secondary">
              {full}
            </li>
          ))}
          {more > 0 && (
            <li className="text-[12px] text-muted-foreground">
              and {more} more
            </li>
          )}
        </ul>
      ) : (
        <p className="mt-1 text-[12px] text-muted-foreground">
          No repositories listed. Add the ones you want agents working in.
        </p>
      )}
    </div>
  )
}

/**
 * The scope guidance, shown on the buttons that lead to the INSTALL flow —
 * which is the only screen where the choice it describes actually appears.
 * Putting it on the authorize button (the first version) described a picker the
 * user was never shown.
 */
function ScopeAdvice() {
  return (
    <p className="text-[13px] text-muted-foreground">
      GitHub will ask which repositories to grant. Choose{' '}
      <span className="font-medium text-foreground">Only select repositories</span> and pick
      just the ones you want agents working in — canopy cannot create or delete
      repositories, so it only ever reaches what you list here.
    </p>
  )
}

/**
 * A real anchor, not a Button with onClick: this is a full-page navigation out
 * of the SPA (GitHub has to show its own consent screen), and a link keeps
 * middle-click and "copy link" working.
 */
function ConnectLink({ label }: { label: string }) {
  return (
    <a
      href={gitHubConnectPath()}
      className="inline-flex items-center rounded bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90"
    >
      {label}
    </a>
  )
}

function Card({ children }: { children: React.ReactNode }) {
  return <div className="rounded-xl border border-border bg-card p-5 space-y-4">{children}</div>
}

function Note({
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
