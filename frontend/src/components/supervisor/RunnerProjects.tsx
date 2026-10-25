import { useCallback, useEffect, useRef, useState, type JSX } from 'react'
import clsx from 'clsx'
import type { AgentOut } from '@/api/agents'
import {
  addRunnerProject,
  deleteRunnerProjectRequest,
  listRunnerProjects,
  type RunnerProjectRequest,
  type RunnerProjects as RunnerProjectsData,
} from '@/api/harness'

// "Add a repo to this runner" (canopy-web `runner_projects`): a laptop runs an
// agent in the emdash project named after it, and routing skips a laptop without
// one. Asking here puts the ask on the box's heartbeat; the box clones the repo
// under its own account, adds it in emdash, and reports back. These components
// show the ask until the box answers.

const POLL_MS = 5000

/** One runner's projects and asks, re-read every few seconds while one is pending. */
export function useRunnerProjects(runnerId: string | null): {
  data: RunnerProjectsData | null
  error: string | null
  reload: () => void
} {
  const [data, setData] = useState<RunnerProjectsData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const live = useRef(true)
  const reload = useCallback(() => {
    if (!runnerId) return
    listRunnerProjects(runnerId)
      .then((d) => { if (live.current) { setData(d); setError(null) } })
      .catch((e: unknown) => { if (live.current) setError(e instanceof Error ? e.message : 'could not load its projects') })
  }, [runnerId])
  useEffect(() => {
    live.current = true
    setData(null)
    reload()
    return () => { live.current = false }
  }, [reload])
  const pending = data?.requests.some((r) => r.status === 'pending') ?? false
  useEffect(() => {
    if (!pending) return
    const t = setInterval(reload, POLL_MS)
    return () => clearInterval(t)
  }, [pending, reload])
  return { data, error, reload }
}

function StatusText({ request }: { request: RunnerProjectRequest }): JSX.Element {
  const label = request.status === 'pending' ? 'adding…' : request.status === 'added' ? 'added' : 'failed'
  return (
    <span
      className={clsx(
        'shrink-0 text-[11px]',
        request.status === 'added' && 'text-success',
        request.status === 'failed' && 'text-destructive',
        request.status === 'pending' && 'text-warning',
      )}
      data-testid={`runner-project-status-${request.name}`}
    >
      {label}
    </span>
  )
}

/** "Add it there now" for one (runner, agent) pair, and how the ask is going.
 *  Used wherever a laptop is shown lacking an agent's repo. */
export function AddAgentRepoButton({
  runnerId,
  agentSlug,
}: {
  runnerId: string
  agentSlug: string
}): JSX.Element {
  const { data, error, reload } = useRunnerProjects(runnerId)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const request = data?.requests.find((r) => r.name === agentSlug)
  const held = data?.projects.includes(agentSlug) ?? false
  const ask = () => {
    setBusy(true)
    setErr(null)
    addRunnerProject(runnerId, { agent: agentSlug })
      .then(reload)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not ask it'))
      .finally(() => setBusy(false))
  }
  if (error) return <span className="text-[11px] text-muted-foreground">{error}</span>
  if (!data) return <span className="text-[11px] text-muted-foreground">…</span>
  if (held) return <span className="text-[11px] text-success" data-testid={`add-agent-repo-held-${runnerId}`}>has it now</span>
  return (
    <span className="inline-flex flex-wrap items-center gap-2" data-testid={`add-agent-repo-${runnerId}`}>
      {request && <StatusText request={request} />}
      {request?.status === 'failed' && request.detail && (
        <span className="text-[11px] text-destructive">{request.detail}</span>
      )}
      {data.can_add && request?.status !== 'pending' && (
        <button
          type="button"
          disabled={busy}
          onClick={ask}
          className="text-[12px] text-primary hover:underline disabled:opacity-50"
          data-testid={`add-agent-repo-go-${runnerId}`}
        >
          {request?.status === 'failed' ? 'Try again' : 'Add it there now'}
        </button>
      )}
      {!data.can_add && !request && (
        <span className="text-[11px] text-muted-foreground">only its owner or an administrator can ask it</span>
      )}
      {err && <span className="text-[11px] text-destructive">{err}</span>}
    </span>
  )
}

/** A laptop's emdash projects, every ask to add one, and (for its owner or an
 *  administrator) a form to add an agent's repo or any repo. */
export function RunnerProjects({
  runnerId,
  agents,
}: {
  runnerId: string
  agents?: readonly AgentOut[] | null
}): JSX.Element {
  const { data, error, reload } = useRunnerProjects(runnerId)
  const [agent, setAgent] = useState('')
  const [repoUrl, setRepoUrl] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  if (error) return <p className="text-[12px] text-destructive">{error}</p>
  if (!data) return <div className="h-6 w-48 animate-pulse rounded-md bg-muted" />

  const missing = (agents ?? []).filter((a) => !data.projects.includes(a.slug))
  const submit = (ask: { agent?: string; repoUrl?: string; name?: string }) => {
    setBusy(true)
    setErr(null)
    addRunnerProject(runnerId, ask)
      .then(() => { setAgent(''); setRepoUrl(''); setName(''); reload() })
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not ask it'))
      .finally(() => setBusy(false))
  }
  const forget = (r: RunnerProjectRequest) => {
    deleteRunnerProjectRequest(runnerId, r.id).then(reload).catch((e: unknown) =>
      setErr(e instanceof Error ? e.message : 'could not remove it'))
  }
  const input = 'h-7 rounded-md border border-input bg-input px-1.5 text-[12px] text-foreground'

  return (
    <div className="flex flex-col gap-2" data-testid="runner-projects">
      <p className="text-[12px] text-muted-foreground">
        Its emdash projects: {data.projects.length ? (
          <span className="font-mono text-foreground">{data.projects.join(', ')}</span>
        ) : 'none reported'}
        . A laptop takes an agent&rsquo;s work only with a project named after it.
      </p>
      {data.requests.length > 0 && (
        <table className="w-full text-[12px]">
          <tbody>
            {data.requests.map((r) => (
              <tr key={r.id} className="border-t border-border align-top" data-testid={`runner-project-${r.name}`}>
                <td className="py-1 pr-2 font-mono text-foreground">{r.name}</td>
                <td className="py-1 pr-2"><StatusText request={r} /></td>
                <td className="py-1 pr-2 text-muted-foreground">
                  <span className="break-all">{r.detail || r.repo_url}</span>
                </td>
                <td className="py-1 text-right">
                  {data.can_add && (
                    <span className="inline-flex gap-2">
                      {r.status === 'failed' && (
                        <button type="button" className="text-primary hover:underline"
                          onClick={() => submit(r.agent ? { agent: r.agent } : { repoUrl: r.repo_url, name: r.name })}>
                          Retry
                        </button>
                      )}
                      <button type="button" className="text-muted-foreground hover:text-foreground"
                        onClick={() => forget(r)}>
                        {r.status === 'pending' ? 'Withdraw' : 'Clear'}
                      </button>
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {data.can_add && (
        <div className="flex flex-col gap-1.5 rounded-md border border-border bg-card p-2">
          {missing.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[12px] text-muted-foreground">An agent&rsquo;s repo:</span>
              <select value={agent} onChange={(e) => setAgent(e.target.value)} className={input}
                data-testid="runner-projects-agent">
                <option value="">Pick an agent…</option>
                {missing.map((a) => <option key={a.slug} value={a.slug}>{a.name} ({a.slug})</option>)}
              </select>
              <button type="button" disabled={busy || !agent} onClick={() => submit({ agent })}
                className="text-[12px] text-primary hover:underline disabled:opacity-50"
                data-testid="runner-projects-add-agent">
                Add
              </button>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[12px] text-muted-foreground">Any repo:</span>
            <input value={repoUrl} onChange={(e) => setRepoUrl(e.target.value)} placeholder="https://github.com/org/repo"
              className={clsx(input, 'min-w-0 flex-1')} data-testid="runner-projects-url" />
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="name (optional)"
              className={clsx(input, 'w-32')} data-testid="runner-projects-name" />
            <button type="button" disabled={busy || !repoUrl.trim()}
              onClick={() => submit({ repoUrl: repoUrl.trim(), name: name.trim() })}
              className="text-[12px] text-primary hover:underline disabled:opacity-50"
              data-testid="runner-projects-add-url">
              Add
            </button>
          </div>
          <p className="text-[11px] text-muted-foreground">
            The runner clones it with its own git sign-in, adds it to emdash, and reports back here. A repo that is
            not an agent&rsquo;s also needs to be one you use canopy in (your Settings → Opted-in repos) before work routes to it.
          </p>
        </div>
      )}
      {err && <p className="text-[12px] text-destructive" data-testid="runner-projects-error">{err}</p>}
    </div>
  )
}
