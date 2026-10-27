import { useEffect, useState } from 'react'

import {
  type AgentGitHub,
  checkAgentGitHub,
  deleteAgentGitHub,
  getAgentGitHub,
  setAgentGitHub,
} from '@/api/agents'
import { useAuth } from '@/auth/AuthProvider'
import { GitHubTokenSteps } from '@/components/agents/GitHubTokenSteps'
import type { CredStatus } from '@/pages/agents/agentCredentials'

// GitHub is a PERSON's identity, delegated to this one agent — its owner's, or one of
// its admins' — not an account of the agent's own. The person makes a fine-grained token
// on GitHub whose repository selection IS what this agent may touch and pastes it here;
// THIS browser checks it against GitHub and seals it so that only the machines that run
// the agent can open it (@/vault/githubDelegation); canopy-web never receives the token.
// Nobody can read it in canopy afterwards, the person included: they replace or withdraw it. The server says who may delegate
// (`can_lend`), whose token is in force (`lent_by_email`) and which machines can open it.
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

export function AgentGitHubSection({ slug, onStatus }: { slug: string; onStatus?: (s: CredStatus) => void }) {
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

  useEffect(() => {
    if (!gh) return
    const failing = (gh.checks ?? []).some((c) => !c.ok)
    onStatus?.(
      !gh.set
        ? { label: 'Not set', tone: 'muted' }
        : gh.expired
          ? { label: 'Expired', tone: 'destructive' }
          : gh.error || failing || gh.no_longer_works
            ? { label: 'Not working', tone: 'destructive' }
            : gh.expiring_soon
              ? { label: 'Expires soon', tone: 'warning' }
              : { label: 'Working', tone: 'success' },
    )
  }, [gh, onStatus])

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
  const lentByMe = Boolean(user?.email && gh.lent_by_email && user.email === gh.lent_by_email)
  const failing = (gh.checks ?? []).filter((c) => !c.ok)
  const broken = Boolean(gh.error || failing.length || gh.no_longer_works)
  const tone = !gh.set || gh.expired || broken ? 'destructive' : gh.expiring_soon ? 'warning' : 'success'
  const machines = gh.machines ?? []
  const eligible = gh.eligible_machines ?? []
  // "browser" / "server": checked when it was given, not by a machine that opened it.
  const checkedBy = gh.checked_by && !['server', 'browser'].includes(gh.checked_by) ? gh.checked_by : ''

  return (
    <section id="cred-github" className="mb-5 scroll-mt-6" data-testid="agent-github">
      <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        GitHub — a delegated credential
      </h3>
      <p className="mb-2 max-w-2xl text-[11px] text-muted-foreground">
        Pushes and opens pull requests with a token a person delegated from their own GitHub
        account — its owner{gh.owner_email ? <> ({gh.owner_email})</> : null} or one of its admins —
        on the repositories that token allows, in any turn canopy lets run its full profile. Only the
        machines that run {slug} can open it; nobody can read it in canopy.
      </p>
      <div className="rounded-lg border border-border bg-card px-3 py-2 text-[12px]">
        <div className="flex flex-wrap items-center gap-2" data-testid="github-status">
          <span className={`rounded border px-1.5 py-0.5 text-[11px] ${TONE[tone]}`}>
            {!gh.set ? 'Not set' : gh.expired ? 'Expired' : broken ? 'Not working' : gh.expiring_soon ? 'Expires soon' : 'Working'}
          </span>
          {gh.set && (
            <span className="text-foreground-secondary">
              acts as <span className="font-mono">@{gh.login}</span>
              {gh.lent_by_email ? <> · delegated by {gh.lent_by_email}</> : null}
              {gh.expires_at ? (
                <> · GitHub says it {gh.expired ? 'expired' : 'expires'} {day(gh.expires_at)}</>
              ) : (
                <> · GitHub sets no expiry</>
              )}
              {gh.checked_at ? (
                <>
                  {' '}
                  · checked {day(gh.checked_at)}
                  {checkedBy ? <> by {checkedBy}</> : null}
                </>
              ) : null}
            </span>
          )}
          {gh.set && !gh.in_vault && (
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
        {gh.no_longer_works && (
          <p className="mt-1 text-destructive">
            A machine found this token no longer works — {gh.lent_by_email || 'its person'} replaces it.
          </p>
        )}
        {gh.in_vault && (
          <p className="mt-1 text-[11px] text-muted-foreground" data-testid="github-machines">
            Only these machines can open it: {machines.length ? machines.join(', ') : 'none yet'}
            {(gh.waiting_machines ?? []).length ? (
              <> · waiting for one of them to seal it to {(gh.waiting_machines ?? []).join(', ')}</>
            ) : null}
            .
          </p>
        )}
        {gh.set && (
          <p className="mt-1 text-[11px] text-muted-foreground" data-testid="github-local-use">
            Someone using {slug} on their own machine does not get this token: there it pushes and
            opens pull requests as their own GitHub account.
          </p>
        )}
        {(gh.identity_for_repos ?? []).length > 0 && (
          <p className="mt-1 text-[11px] text-muted-foreground" data-testid="github-identity-for-repos">
            Agentless turns on{' '}
            {(gh.identity_for_repos ?? []).map((r, i) => (
              <span key={r}>
                {i > 0 ? ', ' : null}
                <span className="font-mono">{r}</span>
              </span>
            ))}{' '}
            also run as this identity.
          </p>
        )}

        {gh.can_lend ? (
          <div className="mt-3 border-t border-border pt-2" data-testid="github-setup">
            <GitHubTokenSteps tokenUrl={gh.create_url} repo={gh.repo} />
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
              {lentByMe && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void run(() => deleteAgentGitHub(slug))}
                  className="min-h-11 rounded-md border border-input px-3 py-1 text-[12px] text-foreground disabled:opacity-40 sm:min-h-0"
                >
                  Withdraw
                </button>
              )}
            </div>
            <p className="mt-1 max-w-2xl text-[11px] text-muted-foreground" data-testid="github-who-can-open">
              {eligible.length ? (
                <>
                  Checked against GitHub by this browser, then sealed here so only the machines that run{' '}
                  {slug} can open it: {eligible.join(', ')}. canopy never receives the token. Their owners, who
                  are {slug}&rsquo;s admins, could extract it from those machines. Nobody can read it in canopy,
                  including you.
                </>
              ) : (
                <>
                  No machine that runs {slug} can open a delegated token yet: a runner that serves it, owned
                  by one of its admins, needs an approved device key first.
                </>
              )}
            </p>
          </div>
        ) : (
          !gh.set && (
            <p className="mt-2 text-[11px] text-muted-foreground">
              Only the agent&rsquo;s owner{gh.owner_email ? <> ({gh.owner_email})</> : null} or one of its
              admins can delegate their GitHub identity to it.
            </p>
          )
        )}
        {error && <p className="mt-2 text-destructive" role="alert">{error}</p>}
      </div>
    </section>
  )
}
