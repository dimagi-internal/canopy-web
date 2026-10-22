import { useCallback, useEffect, useState, type JSX, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
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
import { setAgentGitHub } from '@/api/agents'
import { GitHubTokenSteps } from '@/components/agents/GitHubTokenSteps'
import { mailboxHint, useHostedDomains } from '@/components/agents/hostedMail'
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
 * GitHub access is the person's own fine-grained token — the credential every
 * agent turn already uses (apps/agents/delegations.py). They make a blank repo
 * and a token scoped to it; the token pushes the scaffold and then becomes the
 * agent's GitHub identity, so one paste covers both and no app install (which on
 * an org needs an org owner) is involved. Canopy never creates a repo.
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
  const [googleAccount, setGoogleAccount] = useState('')
  const hostedDomains = useHostedDomains(workspace)
  const [stakeholders, setStakeholders] = useState('')
  const [owner, setOwnerState] = useState(rememberedOwner)
  const setOwner = (o: string) => {
    setOwnerState(o)
    remember(o)
  }
  const [token, setToken] = useState('')
  const [repoName, setRepoName] = useState('')
  const [repoEdited, setRepoEdited] = useState(false)

  const effectiveSlug = slugEdited ? slug : suggestSlug(name)
  const effectiveRepo = repoEdited ? repoName : effectiveSlug
  const repo = owner && effectiveRepo ? `${owner}/${effectiveRepo}` : ''

  const [check, setCheck] = useState<RepoCheckOut | null>(null)
  const [checkError, setCheckError] = useState<CreationError | null>(null)
  const [checking, setChecking] = useState(false)
  const runCheck = useCallback(async () => {
    if (!repo || !effectiveSlug) return
    setChecking(true)
    setCheckError(null)
    try {
      setCheck(await checkScaffoldRepo(workspace, effectiveSlug, repo, token.trim()))
    } catch (e) {
      setCheck(null)
      setCheckError(e instanceof CreationError ? e : new CreationError(String(e)))
    } finally {
      setChecking(false)
    }
  }, [repo, effectiveSlug, workspace, token])
  // A changed repo or token makes the last answer stale, so ask again once typing
  // settles. Reads only; even with no token the answer carries the links to use.
  useEffect(() => {
    setCheck(null)
    setCheckError(null)
    if (!repo || !effectiveSlug) return
    const t = window.setTimeout(() => void runCheck(), 600)
    return () => window.clearTimeout(t)
  }, [repo, effectiveSlug, runCheck])
  // The fix for "missing" happens on GitHub in another tab; check again on return.
  useEffect(() => {
    if (check?.state !== 'missing' && check?.state !== 'not_empty') return
    const again = () => void runCheck()
    window.addEventListener('focus', again)
    return () => window.removeEventListener('focus', again)
  }, [check?.state, runCheck])

  const [creating, setCreating] = useState('')
  const [createError, setCreateError] = useState<CreationError | null>(null)
  // Set when the agent exists but a later, non-fatal step was refused: the page
  // stays to say so, with a way on to the agent's setup.
  const [created, setCreated] = useState('')
  // `update`: an earlier attempt (or GitHub's README) is there; only missing files are added.
  const ready = check?.state === 'ready' || check?.state === 'update' || check?.state === 'adopt'
  const aboutDone = Boolean(name.trim() && effectiveSlug && mandate.trim())
  // Said under a disabled Create, so it never looks broken for no reason.
  const missing = [
    !name.trim() && 'a name',
    !effectiveSlug && 'a slug',
    !mandate.trim() && 'a mandate',
    !token.trim() && 'a GitHub token (step 2)',
    token.trim() && !ready && 'a repository the token can push to (step 2)',
  ].filter((m): m is string => Boolean(m))

  const create = async () => {
    const agent = {
      workspace, slug: effectiveSlug, name: name.trim(), mandate: mandate.trim(),
      mailbox: mailbox.trim(), google_account: googleAccount.trim(), stakeholders: stakeholders.trim(),
      repo, token: token.trim(),
    }
    setCreateError(null)
    setCreated('')
    try {
      setCreating(
        check?.state === 'adopt' ? 'Adopting the repository…'
          : check?.state === 'update' ? 'Adding the missing files…' : 'Pushing the scaffold…',
      )
      const pushed = await scaffoldAgent(agent)
      setCreating('Registering the agent…')
      await registerAgent(agent, pushed)
      setCreating('Saving the token as its GitHub identity…')
      const setup = `/w/${workspace}/agents/${agent.slug}/settings?created=${encodeURIComponent(pushed.repo)}#setup`
      // Not fatal: the agent exists now, and its setup checklist shows the GitHub
      // step as still to do, with the same control. But say it was refused, and why.
      try {
        await setAgentGitHub(agent.slug, agent.token)
      } catch (e) {
        const why = e instanceof Error ? e.message : String(e)
        setCreateError(new CreationError(
          `${agent.name} was created, but saving the token as its GitHub identity failed: ${why} ` +
            'Retry it from the GitHub step of its setup checklist.',
        ))
        setCreated(setup)
        return
      }
      navigate(setup)
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
        <Field label="Mailbox (optional — Google or canopy-hosted)" hint={mailboxHint(mailbox, hostedDomains)}>
          <Input value={mailbox} onChange={(e) => setMailbox(e.target.value)}
            placeholder={`${effectiveSlug || 'scout'}@${hostedDomains[0] ?? 'example.org'}`} />
        </Field>
        <Field label="Google account (optional)"
          hint="The Google login it uses for Drive, Docs and gog, when that is not the mailbox — e.g. beside a canopy-hosted address. Blank: the mailbox, if it is Google.">
          <Input value={googleAccount} onChange={(e) => setGoogleAccount(e.target.value)}
            placeholder={`${effectiveSlug || 'scout'}@dimagi-ai.com`} aria-label="Google account" />
        </Field>
        <Field label="Who it serves (optional)">
          <Input value={stakeholders} onChange={(e) => setStakeholders(e.target.value)} placeholder="the contributor team" />
        </Field>
      </Step>

      <Step n={2} title="Where its code lives" done={ready}>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.5fr)]">
          <Field label="Owner" hint="The GitHub account or org it lives under.">
            <Input value={owner} onChange={(e) => setOwner(e.target.value.trim())} placeholder="dimagi-internal" />
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
        {check && (
          <div className="text-[12px] text-foreground-secondary">
            <p className="mb-1">
              <a href={check.new_repo_url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
                Create {check.repo} on GitHub ↗
              </a>{' '}
              if it does not exist — leave it empty: no README, licence or .gitignore. Then give canopy a token
              for it; the same token is what the agent works as on GitHub.
            </p>
            <GitHubTokenSteps tokenUrl={check.token_url} repo={check.repo} />
          </div>
        )}
        <Field label="GitHub token">
          <Input
            type="password"
            autoComplete="off"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="github_pat_…"
          />
        </Field>
        <RepoState
          check={check}
          error={checkError}
          checking={checking}
          onUseSlug={(s) => {
            setSlugEdited(true)
            setSlug(s)
          }}
        />
      </Step>

      <Step n={3} title="Create it" done={false}>
        <p className="mb-3 text-[12px] text-muted-foreground">
          Pushes the template with your token, registers the agent here with you as its owner, and saves the token
          as its GitHub identity. Safe to run again if it stops partway.
        </p>
        <Button onClick={() => void create()} disabled={missing.length > 0 || Boolean(creating)}>
          {creating || 'Create agent'}
        </Button>
        {missing.length > 0 && !creating && (
          <p className="text-[12px] text-muted-foreground">Still needed: {missing.join(', ')}.</p>
        )}
        {createError && <Problem error={createError} />}
        {created && (
          <Button className="mt-2" size="sm" variant="outline" onClick={() => navigate(created)}>
            Open its setup
          </Button>
        )}
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

function RepoState({
  check,
  error,
  checking,
  onUseSlug,
}: {
  check: RepoCheckOut | null
  error: CreationError | null
  checking: boolean
  /** For a repo that already holds another agent: take that agent's slug. */
  onUseSlug: (slug: string) => void
}): JSX.Element | null {
  if (error) return <Problem error={error} />
  if (checking) return <p className="text-[12px] text-muted-foreground">Checking…</p>
  if (!check || check.state === 'no_token') return null
  if (check.state === 'ready' || check.state === 'update' || check.state === 'adopt') {
    return (
      <p className="flex items-center gap-1.5 text-[13px] text-success">
        <CheckCircle2 className="h-4 w-4 shrink-0" aria-hidden="true" />
        {check.state === 'ready' ? `${check.repo} is empty; pushing as @${check.login}.` : check.detail}
      </p>
    )
  }
  if (check.state === 'name_mismatch' && check.suggested_slug) {
    return (
      <div className="flex flex-wrap items-center gap-2 rounded-md border border-warning/40 bg-warning/10 p-3 text-[13px] text-foreground">
        <span>{check.detail}</span>
        <Button size="sm" variant="outline" onClick={() => onUseSlug(check.suggested_slug)}>
          Use “{check.suggested_slug}”
        </Button>
      </div>
    )
  }
  return (
    <p className="rounded-md border border-warning/40 bg-warning/10 p-3 text-[13px] text-foreground">
      {check.detail} This checks again when you come back to the tab.
    </p>
  )
}

function Problem({ error }: { error: CreationError }): JSX.Element {
  return <p className="mt-3 text-[13px] text-destructive">{error.message}</p>
}

/** The owner last typed, per browser — a convenience only, so a blocked storage
 *  read is just an empty field. */
const OWNER_KEY = 'canopy:new-agent-owner'
function rememberedOwner(): string {
  try {
    return window.localStorage.getItem(OWNER_KEY) ?? ''
  } catch {
    return ''
  }
}
function remember(owner: string): void {
  try {
    window.localStorage.setItem(OWNER_KEY, owner)
  } catch {
    // storage blocked; nothing to keep
  }
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
