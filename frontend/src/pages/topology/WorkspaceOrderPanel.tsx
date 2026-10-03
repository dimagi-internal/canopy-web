import { useEffect, useRef, useState, type JSX } from 'react'
import { clsx } from 'clsx'
import { ArrowDown, ArrowUp, X } from 'lucide-react'

import type { RunnerOut } from '@/api/harness'
import { getRunnerOrder, setRunnerOrder, type RunnerOrderRowOut } from '@/api/workspaces'
import { RepoSetupDialog } from '@/components/agents/RepoSetupDialog'
import { roleAllows } from '@/lib/workspaceRoles'
import { useWorkspace } from '@/workspace/WorkspaceProvider'

import type { FleetMap, MapAgent } from './topologyMap'

// A workspace's DEFAULT runner order, edited where its effect is visible. Every
// agent here with no order of its own follows it live — and so does every agent
// in a workspace below that has no order of its own — so replacing a cloud box
// is one edit here, not one per agent (2026-10-03). Repo turns route by it too.
// Owners only: it reaches agents in every division below, and ownership is the
// only role that flows down the tree (the server enforces the same).
export function WorkspaceOrderPanel({
  workspace,
  map,
  fleet,
  onSaved,
  onSelectAgent,
}: {
  workspace: string
  map: FleetMap
  fleet: Map<string, RunnerOut>
  onSaved: () => void
  onSelectAgent: (slug: string) => void
}): JSX.Element {
  const { workspaces } = useWorkspace()
  const canEdit = roleAllows(workspaces.find((w) => w.slug === workspace)?.role, 'own')
  const [rows, setRows] = useState<RunnerOrderRowOut[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState('')
  const [repoFor, setRepoFor] = useState<{ agent: MapAgent; runners: string[] } | null>(null)
  const seq = useRef(0)

  useEffect(() => {
    let off = false
    getRunnerOrder(workspace)
      .then((r) => { if (!off) setRows(r) })
      .catch((e: unknown) => { if (!off) setError(e instanceof Error ? e.message : 'Failed to load') })
    return () => { off = true }
  }, [workspace])

  const save = (next: { runnerId: string; enabled: boolean }[]) => {
    const mine = ++seq.current
    setError(null)
    setRunnerOrder(workspace, next)
      .then((saved) => {
        if (seq.current !== mine) return
        setRows(saved)
        onSaved()
      })
      .catch((e: unknown) => { if (seq.current === mine) setError(e instanceof Error ? e.message : 'Failed to save') })
  }
  const asRows = (rs: readonly RunnerOrderRowOut[]) => rs.map((r) => ({ runnerId: r.runner_id, enabled: r.enabled }))
  const move = (i: number, d: -1 | 1) => {
    if (!rows) return
    const next = asRows(rows)
    const j = i + d
    if (j < 0 || j >= next.length) return
    ;[next[i], next[j]] = [next[j], next[i]]
    save(next)
  }

  const ws = findWs(map, workspace)
  const inheritsFrom = map.orderFrom.get(workspace) ?? null
  // Agents that follow THIS order: in this workspace or below, with no order of
  // their own, and following this workspace's (not a nearer one's).
  const followers = [...map.agents.values()].filter((a) => a.follows?.workspace === workspace)
  const lacking = followers.filter((a) => (a.follows?.missing_repo ?? []).length > 0)
  const listed = new Set((rows ?? []).map((r) => r.runner_id))
  const addable = [...fleet.values()].filter((r) => !listed.has(r.id) && r.status !== 'retired')

  return (
    <div className="flex flex-col gap-3" data-testid="workspace-order-panel">
      <div className="flex flex-col gap-1">
        <h3 className="m-0 text-[16px] font-semibold text-foreground">{ws?.displayName ?? workspace}: default runners</h3>
        <p className="m-0 text-[12px] text-foreground-secondary">
          Every agent here without runners of its own follows this order, live, after its own rules — and so does
          every workspace below without an order of its own. Replacing a box means changing it here once.
        </p>
      </div>

      {rows === null && !error && <div className="h-16 animate-pulse rounded-md bg-muted" />}
      {rows !== null && rows.length === 0 && (
        <p className="m-0 text-[12px] text-muted-foreground">
          {inheritsFrom
            ? `No order of its own: it follows ${inheritsFrom}'s.`
            : 'No order. Agents here without their own runners cannot run.'}
        </p>
      )}
      {rows !== null && rows.length > 0 && (
        <ol className="m-0 flex list-none flex-col gap-1 p-0" data-testid="workspace-order-rows">
          {rows.map((r, i) => (
            <li
              key={r.runner_id}
              className={clsx('flex min-h-9 items-center gap-2 rounded-md border border-border bg-background px-2', !r.enabled && 'opacity-50')}
            >
              <span className="w-4 text-[12px] text-muted-foreground">{i + 1}</span>
              <span className={r.online && r.ready ? 'text-success' : 'text-muted-foreground'} aria-hidden="true">●</span>
              <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-foreground">{r.runner_name}</span>
              <span className="text-[11px] text-muted-foreground">{r.kind}</span>
              {canEdit && (
                <span className="flex items-center gap-0.5">
                  <IconButton label={`Move ${r.runner_name} up`} disabled={i === 0} onClick={() => move(i, -1)}>
                    <ArrowUp className="h-3.5 w-3.5" />
                  </IconButton>
                  <IconButton label={`Move ${r.runner_name} down`} disabled={i === rows.length - 1} onClick={() => move(i, 1)}>
                    <ArrowDown className="h-3.5 w-3.5" />
                  </IconButton>
                  <IconButton label={`Remove ${r.runner_name}`} onClick={() => save(asRows(rows).filter((x) => x.runnerId !== r.runner_id))}>
                    <X className="h-3.5 w-3.5" />
                  </IconButton>
                </span>
              )}
            </li>
          ))}
        </ol>
      )}
      {canEdit && rows !== null && (
        <div className="flex items-center gap-2">
          <select
            value={adding}
            onChange={(e) => setAdding(e.target.value)}
            className="min-h-9 min-w-0 flex-1 rounded-md border border-input bg-input px-2 text-[12px] text-foreground"
            aria-label="Runner to add"
            data-testid="workspace-order-add-select"
          >
            <option value="">Add a runner…</option>
            {addable.map((r) => (
              <option key={r.id} value={r.id}>{r.name} ({r.kind})</option>
            ))}
          </select>
          <button
            type="button"
            disabled={!adding}
            onClick={() => {
              save([...asRows(rows), { runnerId: adding, enabled: true }])
              setAdding('')
            }}
            className="min-h-9 rounded-md bg-primary px-3 text-[12px] font-medium text-primary-foreground disabled:opacity-40"
            data-testid="workspace-order-add"
          >
            Add
          </button>
        </div>
      )}
      {!canEdit && <p className="m-0 text-[12px] text-muted-foreground">Only a workspace owner can change it.</p>}
      {error && <p className="m-0 text-[12px] text-destructive">{error}</p>}

      <section className="flex flex-col gap-1">
        <h4 className="m-0 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Agents following it</h4>
        {followers.length === 0 && <span className="text-[12px] text-muted-foreground">None — every agent here has its own runners.</span>}
        {followers.map((a) => (
          <button
            key={a.slug}
            type="button"
            onClick={() => onSelectAgent(a.slug)}
            className="flex items-baseline justify-between gap-2 text-left text-[13px] hover:underline"
          >
            <span className="text-foreground-secondary">{a.name}</span>
            <span className="text-[11px] text-muted-foreground">{a.workspace}</span>
          </button>
        ))}
      </section>

      {lacking.length > 0 && (
        <section className="flex flex-col gap-1" data-testid="workspace-order-missing-repo">
          <h4 className="m-0 text-[11px] font-medium uppercase tracking-wide text-warning">Laptops without the agent&rsquo;s repo</h4>
          {lacking.map((a) => {
            const names = (a.follows?.missing_repo ?? []).map((id) => map.runners.get(id)?.name ?? id)
            return (
              <div key={a.slug} className="flex items-baseline justify-between gap-2 text-[12px]">
                <span className="text-foreground-secondary">
                  {a.name}: skips {names.join(', ')}
                </span>
                <button type="button" onClick={() => setRepoFor({ agent: a, runners: names })} className="text-primary hover:underline">
                  Get it there…
                </button>
              </div>
            )
          })}
        </section>
      )}
      {repoFor && (
        <RepoSetupDialog
          open
          onClose={() => setRepoFor(null)}
          agentSlug={repoFor.agent.slug}
          agentName={repoFor.agent.name}
          repoUrl={repoFor.agent.repoUrl}
          runners={repoFor.runners}
        />
      )}
    </div>
  )
}

function IconButton({
  label,
  disabled,
  onClick,
  children,
}: {
  label: string
  disabled?: boolean
  onClick: () => void
  children: React.ReactNode
}): JSX.Element {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
      className="inline-flex h-8 w-8 items-center justify-center rounded text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-30"
    >
      {children}
    </button>
  )
}

function findWs(map: FleetMap, slug: string) {
  const walk = (ws: FleetMap['root']): FleetMap['root'] => {
    if (!ws) return null
    if (ws.slug === slug) return ws
    for (const c of ws.children) {
      const hit = walk(c)
      if (hit) return hit
    }
    return null
  }
  return walk(map.root)
}
