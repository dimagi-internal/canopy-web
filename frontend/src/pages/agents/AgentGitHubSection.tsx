import { useEffect, useState } from 'react'

import {
  type AgentGitHub,
  checkAgentGitHub,
  deleteAgentGitHub,
  getAgentGitHub,
  setAgentGitHub,
} from '@/api/agents'
import { useAuth } from '@/auth/AuthProvider'
import { Button } from 'canopy-ui/ui'
import { AccountRow, type PillTone } from '@/pages/agents/AccountRow'

// GitHub is a PERSON's identity, delegated to this one agent — its owner's, or one of
// its admins' — not an account of the agent's own. The person makes a fine-grained token
// on GitHub whose repository selection IS what this agent may touch and pastes it here;
// canopy checks it, then seals it so that only the machines that run the agent can open
// it (apps/agents/credentials/accounts.py). Nobody can read it in canopy afterwards, the
// person included: they replace or withdraw it. The server says who may delegate
// (`can_lend`), whose token is in force (`lent_by_email`) and which machines can open it.
//
// canopy cannot mint the token: GitHub has no API that creates a fine-grained
// token, only a form that accepts pre-filled values. So the button below opens
// that form filled in, and the one thing it cannot fill — which repositories — is
// said out loud. The server then refuses a token that cannot open a pull request
// on the agent's repo, which is how the commonest mistake (the resource owner
// quietly falling back to a personal account) is caught at paste time.

function day(iso?: string | null): string {
  return iso ? new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : ''
}

export function AgentGitHubSection({ slug }: { slug: string }) {
  const { user } = useAuth()
  const [gh, setGh] = useState<AgentGitHub | null>(null)
  const [token, setToken] = useState('')
  const [open, setOpen] = useState(false)
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
      setOpen(false)
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
  const pill: { label: string; tone: PillTone } = !gh.set
    ? { label: 'Not set', tone: 'neutral' }
    : gh.expired
      ? { label: 'Expired', tone: 'destructive' }
      : broken
        ? { label: 'Not working', tone: 'destructive' }
        : gh.expiring_soon
          ? { label: 'Expires soon', tone: 'warning' }
          : { label: 'Working', tone: 'success' }
  const eligible = gh.eligible_machines ?? []
  const detail = gh.set ? (
    <>
      acts as <span className="font-mono">@{gh.login}</span>
      {gh.expires_at ? <> · {gh.expired ? 'expired' : 'expires'} {day(gh.expires_at)}</> : <> · no expiry</>}
    </>
  ) : null

  return (
    <AccountRow
      id="cred-github"
      testId="agent-github"
      name="GitHub"
      pill={pill}
      detail={detail ? <span data-testid="github-status">{detail}</span> : null}
      action={
        gh.can_lend ? (
          <Button size="sm" variant={gh.set ? 'outline' : 'default'} onClick={() => setOpen((o) => !o)} aria-expanded={open}>
            {gh.set ? 'Replace token' : 'Connect GitHub'}
          </Button>
        ) : null
      }
    >
      {failing.length > 0 && (
        <ul className="space-y-0.5" data-testid="github-checks">
          {failing.map((c) => (
            <li key={c.repo} className="text-destructive">
              <span className="font-mono">{c.repo}</span> — {c.detail}
            </li>
          ))}
        </ul>
      )}
      {gh.error && <p className="text-destructive">{gh.error}</p>}
      {gh.no_longer_works && (
        <p className="text-destructive">
          A machine found this token no longer works — {gh.lent_by_email || 'its person'} replaces it.
        </p>
      )}
      {(gh.identity_for_repos ?? []).length > 0 && (
        <p data-testid="github-identity-for-repos">
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
      {!gh.can_lend && !gh.set && (
        <p>
          Only the agent&rsquo;s owner{gh.owner_email ? <> ({gh.owner_email})</> : null} or one of its
          admins can delegate their GitHub identity to it.
        </p>
      )}
      {gh.can_lend && open && (
        <div className="space-y-2 pt-1" data-testid="github-setup">
          <p>
            <a href={gh.create_url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
              Create the token on GitHub
            </a>
          </p>
          <p>
            On that page, pick {gh.repo ? <span className="font-mono">{gh.repo}</span> : 'this agent’s repo'} and any
            other repo it ships to, then generate it and paste it here.
          </p>
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
            <Button size="sm" disabled={busy || !token.trim()} onClick={() => void run(() => setAgentGitHub(slug, token))}>
              {busy ? 'Checking…' : 'Save'}
            </Button>
            {gh.set && !gh.in_vault && (
              <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(() => checkAgentGitHub(slug))}>
                Check now
              </Button>
            )}
            {lentByMe && (
              <Button size="sm" variant="outline" disabled={busy} onClick={() => void run(() => deleteAgentGitHub(slug))}>
                Withdraw
              </Button>
            )}
          </div>
          {eligible.length === 0 && (
            <p data-testid="github-who-can-open">
              No machine that runs {slug} can open a delegated token yet: a runner that serves it, owned by one of
              its admins, needs an approved device key first.
            </p>
          )}
        </div>
      )}
      {error && <p className="text-destructive" role="alert">{error}</p>}
    </AccountRow>
  )
}
