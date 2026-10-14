import { useCallback, useEffect, useState, type JSX, type ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { CheckCircle2 } from 'lucide-react'
import { Button, Input, Textarea } from 'canopy-ui/ui'
import { WorkbenchSubHeader } from 'canopy-ui'

import {
  checkScaffoldRepo,
  CreationError,
  registerAgent,
  scaffoldAgent,
  type RepoCheckOut,
} from '@/api/agentCreation'
import {
  getGitHubConnection,
  gitHubConnectPath,
  listGitHubInstallations,
  type GitHubConnectionOut,
  type GitHubInstallationOut,
} from '@/api/github'
import { roleAllows } from '@/lib/workspaceRoles'
import { useWorkspaceRole } from '@/workspace/WorkspaceProvider'

/**
 * Create an agent — the web half of `/canopy:create-agent`.
 *
 * The split is by what each tool is good at. The web does the plumbing, which
 * is mechanical and has one right answer: stamp the repo from the same factory
 * the terminal uses, push it, register the agent. It then lands on the agent's
 * setup checklist, which links each remaining control (GitHub identity,
 * mailbox, runner). The part with no right answer — who the agent is and what
 * its first skill does — is a conversation, so it happens in Claude Code, and
 * the checklist hands over the command that starts it.
 *
 * Canopy cannot create the repository: its GitHub App deliberately holds no
 * Administration permission (apps/tokens/github_app.py). So the person makes a
 * blank repo and adds it to canopy's access; this page links both and re-checks
 * when they come back to the tab.
 */
export function NewAgentPage(): JSX.Element {
  const { workspace = '' } = useParams<{ workspace: string }>()
  const navigate = useNavigate()
  const canCreate = roleAllows(useWorkspaceRole(workspace), 'agent.work')

  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [slugEdited, setSlugEdited] = useState(false)
  const [mandate, setMandate] = useState('')
  const [mailbox, setMailbox] = useState('')
  const [stakeholders, setStakeholders] = useState('')
  const [owner, setOwner] = useState('')
  const [repoName, setRepoName] = useState('')
  const [repoEdited, setRepoEdited] = useState(false)

  const effectiveSlug = slugEdited ? slug : suggestSlug(name)
  const effectiveRepo = repoEdited ? repoName : effectiveSlug
  const repo = owner && effectiveRepo ? `${owner}/${effectiveRepo}` : ''

  const github = useGitHub()
  useEffect(() => {
    if (!owner && github.installs?.length) setOwner(github.installs[0].account_login)
  }, [github.installs, owner])

  const [check, setCheck] = useState<RepoCheckOut | null>(null)
  const [checkError, setCheckError] = useState<CreationError | null>(null)
  const [checking, setChecking] = useState(false)
  const runCheck = useCallback(async () => {
    if (!repo || !effectiveSlug) return
    setChecking(true)
    setCheckError(null)
    try {
      setCheck(await checkScaffoldRepo(workspace, effectiveSlug, repo))
    } catch (e) {
      setCheck(null)
      setCheckError(e instanceof CreationError ? e : new CreationError(String(e)))
    } finally {
      setChecking(false)
    }
  }, [repo, effectiveSlug, workspace])
  // A changed name or owner makes the last answer about a different repo, so ask
  // again once typing settles. Reads only; the answer carries the links to fix it.
  useEffect(() => {
    setCheck(null)
    setCheckError(null)
    if (!repo || !effectiveSlug) return
    const t = window.setTimeout(() => void runCheck(), 600)
    return () => window.clearTimeout(t)
  }, [repo, effectiveSlug, runCheck])
  // The fix for "missing" happens on GitHub in another tab; check again on return.
  useEffect(() => {
    if (check?.state !== 'missing') return
    const again = () => void runCheck()
    window.addEventListener('focus', again)
    return () => window.removeEventListener('focus', again)
  }, [check?.state, runCheck])

  const [creating, setCreating] = useState('')
  const [createError, setCreateError] = useState<CreationError | null>(null)
  const ready = check?.state === 'ready' || check?.state === 'adopt'
  const aboutDone = Boolean(name.trim() && effectiveSlug && mandate.trim())
  // Said under a disabled Create, so it never looks broken for no reason.
  const missing = [
    !name.trim() && 'a name',
    !effectiveSlug && 'a slug',
    !mandate.trim() && 'a mandate',
    !ready && 'a repository canopy can push to (step 2)',
  ].filter((m): m is string => Boolean(m))

  const create = async () => {
    const agent = {
      workspace, slug: effectiveSlug, name: name.trim(), mandate: mandate.trim(),
      mailbox: mailbox.trim(), stakeholders: stakeholders.trim(), repo,
    }
    setCreateError(null)
    try {
      setCreating(check?.state === 'adopt' ? 'Adopting the repository…' : 'Pushing the scaffold…')
      const pushed = await scaffoldAgent(agent)
      setCreating('Registering the agent…')
      await registerAgent(agent, pushed)
      navigate(`/w/${workspace}/agents/${agent.slug}/settings?created=${encodeURIComponent(pushed.repo)}#setup`)
    } catch (e) {
      setCreateError(e instanceof CreationError ? e : new CreationError(String(e)))
    } finally {
      setCreating('')
    }
  }

  if (!canCreate) {
    return (
      <div className="max-w-3xl px-6 py-8">
        <WorkbenchSubHeader title="New agent" />
        <p className="text-[13px] text-muted-foreground">
          Creating an agent needs the editor role in this workspace. Ask a workspace owner.
        </p>
      </div>
    )
  }

  return (
    <div className="max-w-3xl px-6 py-8">
      <WorkbenchSubHeader title="New agent" />
      <p className="-mt-2 mb-6 text-[13px] text-foreground-secondary">
        Makes its GitHub repository and registers it here. Its persona and first skill come after, in Claude Code.
      </p>

      <Step n={1} title="Who it is" done={aboutDone}>
        <Field label="Name">
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Scout" />
        </Field>
        <Field label="Slug" hint={`Its id everywhere, and its Slack command: /${effectiveSlug || 'slug'}. Unique across canopy.`}>
          <Input
            value={effectiveSlug}
            onChange={(e) => {
              setSlugEdited(true)
              setSlug(e.target.value)
            }}
            placeholder="scout"
          />
        </Field>
        <Field label="Mandate" hint="One sentence: what it is for.">
          <Textarea
            value={mandate}
            onChange={(e) => setMandate(e.target.value)}
            rows={2}
            placeholder="Find good first issues for new contributors."
          />
        </Field>
        <Field label="Mailbox (optional)" hint="An address it sends and reads mail as. You sign it in with Google later.">
          <Input value={mailbox} onChange={(e) => setMailbox(e.target.value)} placeholder="scout@example.org" />
        </Field>
        <Field label="Who it serves (optional)">
          <Input value={stakeholders} onChange={(e) => setStakeholders(e.target.value)} placeholder="the contributor team" />
        </Field>
      </Step>

      <Step n={2} title="Where its code lives" done={ready}>
        <GitHubStatus github={github} />
        {github.installs && github.installs.length > 0 && (
          <>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.5fr)]">
              <Field label="Owner">
                <select
                  value={owner}
                  onChange={(e) => setOwner(e.target.value)}
                  className="h-9 w-full rounded-md border border-input bg-input px-2 text-[13px] text-foreground"
                >
                  {github.installs.map((i) => (
                    <option key={i.installation_id} value={i.account_login}>
                      {i.account_login}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Repository name">
                <Input
                  value={effectiveRepo}
                  onChange={(e) => {
                    setRepoEdited(true)
                    setRepoName(e.target.value)
                  }}
                />
              </Field>
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-3">
              <Button size="sm" variant="outline" onClick={() => void runCheck()} disabled={!repo || !effectiveSlug || checking}>
                {checking ? 'Checking…' : 'Check again'}
              </Button>
            </div>
            <RepoState check={check} error={checkError} />
          </>
        )}
      </Step>

      <Step n={3} title="Create it" done={false}>
        <p className="mb-3 text-[12px] text-muted-foreground">
          Pushes the template as you, then registers the agent here with you as its owner. Safe to run again if it
          stops partway.
        </p>
        <Button onClick={() => void create()} disabled={missing.length > 0 || Boolean(creating)}>
          {creating || 'Create agent'}
        </Button>
        {missing.length > 0 && !creating && (
          <p className="text-[12px] text-muted-foreground">Still needed: {missing.join(', ')}.</p>
        )}
        {createError && <Problem error={createError} />}
      </Step>
    </div>
  )
}

/** A suggestion only — the server's factory normalises and is the authority. */
export function suggestSlug(name: string): string {
  return name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 31)
}

interface GitHubState {
  conn: GitHubConnectionOut | null
  installs: GitHubInstallationOut[] | null
  error: string
  reload: () => void
}

/** The person's GitHub connection and where the app is installed, re-read when
 *  they come back from connecting in another tab. */
function useGitHub(): GitHubState {
  const [conn, setConn] = useState<GitHubConnectionOut | null>(null)
  const [installs, setInstalls] = useState<GitHubInstallationOut[] | null>(null)
  const [error, setError] = useState('')
  const reload = useCallback(() => {
    setError('')
    getGitHubConnection()
      .then((c) => {
        setConn(c)
        if (!c.connected || c.needs_reconnect) return setInstalls(null)
        return listGitHubInstallations().then(setInstalls)
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
  }, [])
  useEffect(() => {
    reload()
    window.addEventListener('focus', reload)
    return () => window.removeEventListener('focus', reload)
  }, [reload])
  return { conn, installs, error, reload }
}

function GitHubStatus({ github }: { github: GitHubState }): JSX.Element | null {
  const { conn, installs, error } = github
  if (error) return <p className="text-[13px] text-destructive">{error}</p>
  if (!conn) return <p className="text-[13px] text-muted-foreground">Checking your GitHub connection…</p>
  if (!conn.configured) {
    return <p className="text-[13px] text-muted-foreground">GitHub is not set up on this deployment. Ask an administrator.</p>
  }
  const connect = (
    <a href={gitHubConnectPath()} target="_blank" rel="noreferrer" className="text-primary hover:underline">
      {conn.connected && !conn.needs_reconnect ? 'Install it on an account ↗' : 'Connect GitHub ↗'}
    </a>
  )
  if (!conn.connected || conn.needs_reconnect) {
    return (
      <p className="mb-3 text-[13px] text-foreground-secondary">
        Canopy pushes the repo as you, through its GitHub app. {connect} — it opens in a new tab; come back here
        when you are done.
      </p>
    )
  }
  if (installs && installs.length === 0) {
    return (
      <p className="mb-3 text-[13px] text-foreground-secondary">
        Connected as @{conn.github_login}, but canopy&rsquo;s GitHub app is not installed on any account yet. {connect}
      </p>
    )
  }
  return installs ? (
    <p className="mb-3 text-[12px] text-muted-foreground">Pushing as @{conn.github_login}.</p>
  ) : null
}

function RepoState({ check, error }: { check: RepoCheckOut | null; error: CreationError | null }): JSX.Element | null {
  if (error) return <Problem error={error} />
  if (!check) return null
  if (check.state === 'ready' || check.state === 'adopt') {
    return (
      <p className="mt-3 flex items-center gap-1.5 text-[13px] text-success">
        <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
        {check.state === 'ready' ? `${check.repo} is empty and canopy can push to it.` : check.detail}
      </p>
    )
  }
  return (
    <div className="mt-3 rounded-md border border-warning/40 bg-warning/10 p-3 text-[13px] text-foreground">
      <p>{check.detail}</p>
      {check.state === 'missing' && (
        <ol className="mt-2 list-decimal pl-5 text-[12px] text-foreground-secondary">
          <li>
            <a href={check.new_repo_url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
              Create {check.repo} on GitHub ↗
            </a>{' '}
            — leave it empty.
          </li>
          <li>
            <a href={check.fix_url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
              Give canopy access to it ↗
            </a>{' '}
            — pick the account, then add the repository.
          </li>
          <li>Come back to this tab; it checks again on its own.</li>
        </ol>
      )}
    </div>
  )
}

function Problem({ error }: { error: CreationError }): JSX.Element {
  return (
    <p className="mt-3 text-[13px] text-destructive">
      {error.message}
      {error.fixUrl &&
        (error.fixUrl.startsWith('http') ? (
          <a href={error.fixUrl} target="_blank" rel="noreferrer" className="ml-2 text-primary hover:underline">
            Fix it ↗
          </a>
        ) : (
          <Link to={error.fixUrl} className="ml-2 text-primary hover:underline">
            Fix it
          </Link>
        ))}
    </p>
  )
}

function Step({ n, title, done, children }: { n: number; title: string; done: boolean; children: ReactNode }) {
  return (
    <section className="mb-6 rounded-lg border border-border bg-card p-4" aria-labelledby={`step-${n}`}>
      <h2 id={`step-${n}`} className="mb-3 flex items-center gap-2 text-[14px] font-semibold text-foreground">
        {done ? (
          <CheckCircle2 className="h-4 w-4 text-success" aria-label="done" />
        ) : (
          <span className="flex h-4 w-4 items-center justify-center rounded-full border border-border text-[10px] text-muted-foreground">
            {n}
          </span>
        )}
        {title}
      </h2>
      <div className="flex flex-col gap-3">{children}</div>
    </section>
  )
}

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[12px] font-medium text-foreground">{label}</span>
      {children}
      {hint && <span className="text-[11px] text-muted-foreground">{hint}</span>}
    </label>
  )
}
