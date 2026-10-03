import { useCallback, useEffect, useState, type JSX } from 'react'
import { Link } from 'react-router-dom'

import { getAgentDefaultOrder, putAgentRunners, type AgentDefaultOrderOut } from '@/api/agents'
import type { RunnerOut } from '@/api/harness'
import { RunnerAssignments } from '@/components/agents/RunnerAssignments'
import { RepoSetupDialog } from '@/components/agents/RepoSetupDialog'

// The "Everything else" row's runners: the agent's OWN ordered list, or — when it
// has none — the default order of its workspace (or the nearest one above it),
// which it follows live. Following is the default for a new agent, so replacing a
// cloud box is one edit to the workspace's order rather than one per agent
// (2026-10-03). Its source rules, the rows above this one, apply either way.
export function AgentDefaultRunners({
  agentSlug,
  agentName,
  workspace,
  fleet,
  onSaved,
}: {
  agentSlug: string
  agentName: string
  workspace?: string
  fleet: readonly RunnerOut[]
  onSaved?: () => void
}): JSX.Element {
  const [state, setState] = useState<AgentDefaultOrderOut | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [repoFor, setRepoFor] = useState<string[] | null>(null)

  const load = useCallback(
    () =>
      getAgentDefaultOrder(agentSlug, workspace)
        .then((s) => {
          setState(s)
          setError(null)
        })
        .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Failed to load')),
    [agentSlug, workspace],
  )
  useEffect(() => {
    void load()
  }, [load])

  const change = (rows: { runnerId: string; enabled: boolean }[]) => {
    setBusy(true)
    putAgentRunners(agentSlug, rows, workspace)
      .then(() => load())
      .then(() => onSaved?.())
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Failed to save'))
      .finally(() => setBusy(false))
  }

  if (state === null) {
    return error ? (
      <span className="text-[11px] text-destructive">{error}</span>
    ) : (
      <div className="h-6 w-full animate-pulse rounded bg-muted" />
    )
  }

  const orderHref = (ws: string) => `/w/${ws}/settings/topology?order=${ws}`

  if (state.own) {
    return (
      <div className="flex flex-col gap-1" data-testid="default-runners-own">
        <RunnerAssignments
          key={`own-${agentSlug}`}
          agentSlug={agentSlug}
          workspace={workspace}
          fleet={fleet}
          onSaved={onSaved}
          agentName={agentName}
          repoUrl={state.repo_url ?? ''}
        />
        {state.workspace && (
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              if (window.confirm(`Drop ${agentName}'s own runners and follow ${state.workspace}'s default order? Its rules are kept.`)) change([])
            }}
            className="self-start text-[11px] text-primary hover:underline disabled:opacity-50"
            data-testid="default-runners-follow"
          >
            Follow {state.workspace}&rsquo;s default instead
          </button>
        )}
        {error && <span className="text-[11px] text-destructive">{error}</span>}
      </div>
    )
  }

  if (!state.workspace) {
    return (
      <span className="text-[11px] text-warning" data-testid="default-runners-none">
        No runners, and no workspace above it has a default order — set one on the fleet map, or{' '}
        <button
          type="button"
          className="text-primary hover:underline"
          onClick={() => setState({ ...state, own: true })}
        >
          give it its own
        </button>
        .
      </span>
    )
  }

  return (
    <div className="flex flex-col gap-1.5" data-testid="default-runners-follows">
      <div className="flex flex-wrap items-center gap-1.5">
        {(state.runners ?? []).map((r, i) => (
          <span
            key={r.runner_id}
            className="inline-flex items-center gap-1 rounded-md border border-border bg-muted/40 px-1.5 py-0.5 text-[11px] text-foreground"
            title={`${r.runner_name} · ${r.kind}`}
          >
            <span className="text-muted-foreground">{i + 1}</span>
            <span className={r.online && r.ready ? 'text-success' : 'text-muted-foreground'} aria-hidden="true">●</span>
            <span className="font-mono">{r.runner_name.replace(/-mbp-cdp$/, '')}</span>
          </span>
        ))}
        {(state.runners ?? []).length === 0 && (
          <span className="text-[11px] text-destructive">none it can use</span>
        )}
      </div>
      <span className="text-[11px] text-muted-foreground">
        Following{' '}
        <Link to={orderHref(state.workspace)} className="text-primary hover:underline">
          {state.workspace}&rsquo;s default order
        </Link>
        .{' '}
        <button
          type="button"
          disabled={busy}
          onClick={() => change((state.runners ?? []).map((r) => ({ runnerId: r.runner_id, enabled: true })))}
          className="text-primary hover:underline disabled:opacity-50"
          data-testid="default-runners-own-copy"
        >
          Give it its own
        </button>
      </span>
      {(state.missing_repo ?? []).length > 0 && (
        <span className="text-[11px] text-warning" data-testid="default-runners-missing-repo">
          Skipped: {(state.missing_repo ?? []).join(', ')} {(state.missing_repo ?? []).length === 1 ? 'has' : 'have'} no{' '}
          {agentSlug} repo.{' '}
          <button
            type="button"
            onClick={() => setRepoFor([...(state.missing_repo ?? [])])}
            className="text-primary hover:underline"
            data-testid="default-runners-get-repo"
          >
            Get it there…
          </button>
        </span>
      )}
      {(state.cannot_hold ?? []).length > 0 && (
        <span className="text-[11px] text-muted-foreground">
          Skipped: {(state.cannot_hold ?? []).join(', ')} — its owner is not one of {agentName}&rsquo;s admins.
        </span>
      )}
      {error && <span className="text-[11px] text-destructive">{error}</span>}
      <RepoSetupDialog
        open={repoFor !== null}
        onClose={() => setRepoFor(null)}
        agentSlug={agentSlug}
        agentName={agentName}
        repoUrl={state.repo_url ?? ''}
        runners={repoFor ?? []}
      />
    </div>
  )
}
