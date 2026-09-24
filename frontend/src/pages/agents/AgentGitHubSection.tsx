import { useEffect, useState } from 'react'

import {
  type AgentGitHub,
  checkAgentGitHub,
  deleteAgentGitHub,
  getAgentGitHub,
  setAgentGitHub,
} from '@/api/agents'
import { useAuth } from '@/auth/AuthProvider'

// GitHub is the OWNER's identity, lent to this one agent — not a secret of the
// agent's (its vault) nor of the workspace (the shared vault). The owner makes a
// fine-grained token on GitHub whose repository selection IS what this agent may
// touch, pastes it here, and each of the agent's turns is handed it for that turn
// alone. Hand the agent to someone else and it stops being used; they lend their
// own. See apps/agents/delegations.py.
//
// canopy cannot mint the token: GitHub has no API that creates a fine-grained
// token, only a form that accepts pre-filled values. So the button below opens
// that form filled in, and the one thing it cannot fill — which repositories — is
// said out loud. The server then refuses a token that cannot open a pull request
// on the agent's repo, which is how the commonest mistake (the resource owner
// quietly falling back to a personal account) is caught at paste time.

// Literal class names: Tailwind only generates what it can read in the source.
const TONE = {
  destructive: 'bg-destructive/10 text-destructive border-destructive/30',
  warning: 'bg-warning/10 text-warning border-warning/30',
  success: 'bg-success/10 text-success border-success/30',
} as const

function day(iso?: string | null): string {
  return iso ? new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : ''
}

export function AgentGitHubSection({ slug }: { slug: string }) {
  const { user } = useAuth()
  const [gh, setGh] = useState<AgentGitHub | null>(null)
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let off = false
    getAgentGitHub(slug)
      .then((g) => !off && setGh(g))
      .catch(() => {})
    return () => {
      off = true
    }
  }, [slug])

  const run = async (fn: () => Promise<AgentGitHub>) => {
    setBusy(true)
    setError(null)
    try {
      setGh(await fn())
      setToken('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Something went wrong')
    } finally {
      setBusy(false)
    }
  }

  if (!gh) return null
  const isOwner = Boolean(user?.email && gh.owner_email && user.email === gh.owner_email)
  const failing = (gh.checks ?? []).filter((c) => !c.ok)
  const tone = !gh.set || gh.expired || gh.error || failing.length ? 'destructive' : gh.expiring_soon ? 'warning' : 'success'

  return (
    <section className="mb-5" data-testid="agent-github">
      <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        GitHub — acting as its owner
      </h3>
      <p className="mb-2 max-w-2xl text-[11px] text-muted-foreground">
        This agent pushes branches and opens pull requests as its owner
        {gh.owner_email ? <> ({gh.owner_email})</> : null}, on exactly the repositories the owner&rsquo;s
        token allows. Each turn is handed the token for that turn only; no box holds it.
      </p>
      <div className="rounded-lg border border-border bg-card px-3 py-2 text-[12px]">
        <div className="flex flex-wrap items-center gap-2" data-testid="github-status">
          <span className={`rounded border px-1.5 py-0.5 text-[11px] ${TONE[tone]}`}>
            {!gh.set ? 'Not set' : gh.expired ? 'Expired' : gh.error || failing.length ? 'Not working' : gh.expiring_soon ? 'Expires soon' : 'Working'}
          </span>
          {gh.set && (
            <span className="text-foreground-secondary">
              acts as <span className="font-mono">@{gh.login}</span>
              {gh.expires_at ? <> · expires {day(gh.expires_at)}</> : <> · never expires</>}
              {gh.checked_at ? <> · checked {day(gh.checked_at)}</> : null}
            </span>
          )}
          {gh.set && (
            <button
              type="button"
              disabled={busy}
              onClick={() => void run(() => checkAgentGitHub(slug))}
              className="ml-auto rounded-md border border-input px-2 py-0.5 text-[11px] text-foreground disabled:opacity-40"
            >
              Check now
            </button>
          )}
        </div>

        {gh.set && (gh.checks ?? []).length > 0 && (
          <ul className="mt-1 space-y-0.5" data-testid="github-checks">
            {(gh.checks ?? []).map((c) => (
              <li key={c.repo} className={c.ok ? 'text-muted-foreground' : 'text-destructive'}>
                <span className="font-mono">{c.repo}</span> — {c.detail}
              </li>
            ))}
          </ul>
        )}
        {gh.error && <p className="mt-1 text-destructive">{gh.error}</p>}

        {isOwner ? (
          <div className="mt-3 border-t border-border pt-2" data-testid="github-setup">
            <ol className="mb-2 list-decimal space-y-0.5 pl-4 text-[11px] text-muted-foreground">
              <li>
                <a href={gh.create_url} target="_blank" rel="noreferrer" className="text-primary">
                  Create the token on GitHub
                </a>{' '}
                — the form opens filled in.
              </li>
              <li>
                Check <strong>Resource owner</strong> reads{' '}
                <span className="font-mono">{gh.repo ? gh.repo.split('/')[0] : 'the repo’s org'}</span>.
              </li>
              <li>
                Under <strong>Only select repositories</strong>, pick{' '}
                {gh.repo ? <span className="font-mono">{gh.repo}</span> : 'this agent’s repo'}, plus any other repo
                it ships to or installs a plugin from.
              </li>
              <li>Generate it, and paste it here.</li>
            </ol>
            <div className="flex flex-wrap items-center gap-2">
              <input
                type="password"
                autoComplete="off"
                aria-label="GitHub token"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder={gh.set ? 'set — paste to replace' : 'github_pat_…'}
                className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 font-mono text-[12px] text-foreground sm:min-h-0 sm:w-72"
              />
              <button
                type="button"
                disabled={busy || !token.trim()}
                onClick={() => void run(() => setAgentGitHub(slug, token))}
                className="min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40 sm:min-h-0"
              >
                {busy ? 'Checking…' : 'Save'}
              </button>
              {gh.set && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void run(() => deleteAgentGitHub(slug))}
                  className="min-h-11 rounded-md border border-input px-3 py-1 text-[12px] text-foreground disabled:opacity-40 sm:min-h-0"
                >
                  Remove
                </button>
              )}
            </div>
            <p className="mt-1 text-[11px] text-muted-foreground">
              Checked against GitHub before it is saved. Encrypted at rest, never shown again.
            </p>
          </div>
        ) : (
          !gh.set && (
            <p className="mt-2 text-[11px] text-muted-foreground">
              Only the agent&rsquo;s owner{gh.owner_email ? <> ({gh.owner_email})</> : null} can lend it their GitHub
              identity.
            </p>
          )
        )}
        {error && <p className="mt-2 text-destructive" role="alert">{error}</p>}
      </div>
    </section>
  )
}
