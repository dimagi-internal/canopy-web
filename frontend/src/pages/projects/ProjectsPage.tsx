// /w/:workspace/projects — every project in the workspace as one dense table
// (status, health, lead, lead agent, the other agents, progress, open and
// waiting counts, target). Open by default; All or one status behind the
// filter. "New project" makes a workspace project and opens its page. Each row
// opens the project's one page, `/w/<ws>/projects/<handle>`.
import { useEffect, useMemo, useState, type FormEvent, type JSX } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { Button, Dialog, DialogContent, DialogTitle } from 'canopy-ui/ui'

import { createWorkspaceProject, listProjects, type Project } from '@/api/work'
import { invalidateDirectory, useDirectory } from '@/components/work/useDirectory'
import {
  ProjectFilters,
  ProjectTable,
  filterProjects,
  statusQuery,
  type ProjectSort,
  type ProjectStatusFilter,
} from '@/components/work/ProjectTable'
import { displayAgent } from '@/components/work/meta'
import { roleAllows } from '@/lib/workspaceRoles'
import { useWorkspaceRole } from '@/workspace/WorkspaceProvider'

export function ProjectsPage(): JSX.Element {
  const { workspace = '' } = useParams()
  const role = useWorkspaceRole(workspace)
  const [status, setStatus] = useState<ProjectStatusFilter>('open')
  const [q, setQ] = useState('')
  const [sort, setSort] = useState<ProjectSort>('updated')
  const [params, setParams] = useSearchParams()
  const [creating, setCreating] = useState(params.get('new') === '1')
  // `?new=1` (the command palette's "New project") opened the dialog; drop it from the URL.
  useEffect(() => {
    if (params.get('new') === '1') setParams((p) => { const n = new URLSearchParams(p); n.delete('new'); return n }, { replace: true })
  }, [params, setParams])
  // Stamped with the query it answers, so a filter change never shows the last list's rows as this one's.
  const [data, setData] = useState<{ key: string; rows: Project[] | null; error: string | null } | null>(null)
  const key = `${workspace}|${status}`

  useEffect(() => {
    let cancelled = false
    listProjects(statusQuery(status), workspace)
      .then((rows) => !cancelled && setData({ key, rows, error: null }))
      .catch((e: unknown) => !cancelled && setData({ key, rows: null, error: e instanceof Error ? e.message : 'Could not load projects' }))
    return () => {
      cancelled = true
    }
  }, [workspace, status, key])

  const fresh = data?.key === key ? data : null
  const rows = useMemo(() => filterProjects(fresh?.rows ?? [], { status, q, sort }), [fresh, status, q, sort])

  return (
    <div>
      <div className="mb-3">
        <h1 className="text-xl font-semibold text-foreground">Projects</h1>
        <p className="mt-1 text-[13px] text-muted-foreground">
          Every project in this workspace: who leads it, the agents on it, how far along it is and how it is going.
        </p>
      </div>
      <ProjectFilters status={status} onStatus={setStatus} q={q} onQ={setQ} sort={sort} onSort={setSort}>
        {roleAllows(role, 'agent.work') && (
          <Button size="sm" onClick={() => setCreating(true)}>
            New project
          </Button>
        )}
      </ProjectFilters>
      {fresh?.error && <p className="text-[13px] text-destructive">{fresh.error}</p>}
      {!fresh && <p className="text-[13px] text-muted-foreground">Loading…</p>}
      {fresh?.rows && (
        <ProjectTable
          projects={rows}
          empty={
            fresh.rows.length === 0
              ? status === 'open'
                ? 'No open projects. Start one with New project.'
                : 'No projects here.'
              : 'No project matches.'
          }
        />
      )}
      <NewProjectDialog workspace={workspace} open={creating} onClose={() => setCreating(false)} />
    </div>
  )
}

/** Name, outcome, and optionally its lead agent, lead person and target date. */
export function NewProjectDialog({
  workspace,
  open,
  onClose,
}: {
  workspace: string
  open: boolean
  onClose: () => void
}): JSX.Element {
  const navigate = useNavigate()
  const directory = useDirectory(workspace)
  const [name, setName] = useState('')
  const [outcome, setOutcome] = useState('')
  const [steward, setSteward] = useState('')
  const [owner, setOwner] = useState('')
  const [target, setTarget] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (!name.trim() || busy) return
    setBusy(true)
    setError(null)
    createWorkspaceProject({
      name: name.trim(),
      workspace,
      outcome: outcome.trim(),
      ...(steward ? { steward, member_agents: [steward] } : {}),
      ...(owner ? { owner_email: owner } : {}),
      ...(target ? { target_date: target } : {}),
    })
      .then((p) => {
        invalidateDirectory(workspace)
        onClose()
        navigate(p.url)
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : 'Could not create the project'))
      .finally(() => setBusy(false))
  }

  const field = 'w-full rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground placeholder:text-placeholder'
  return (
    <Dialog open={open} onOpenChange={(o: boolean) => !o && onClose()}>
      <DialogContent className="w-[calc(100%-2rem)] max-w-lg bg-card" data-testid="new-project-dialog">
        <DialogTitle className="text-sm font-semibold text-foreground">New project</DialogTitle>
        <form onSubmit={submit} className="flex flex-col gap-3 text-[13px]">
          <label className="flex flex-col gap-1">
            <span className="text-muted-foreground">Name</span>
            <input value={name} onChange={(e) => setName(e.target.value)} aria-label="Name" className={field} autoFocus />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-muted-foreground">Outcome — what done looks like</span>
            <textarea value={outcome} onChange={(e) => setOutcome(e.target.value)} aria-label="Outcome" rows={2} className={field} />
          </label>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="flex flex-col gap-1">
              <span className="text-muted-foreground">Lead agent</span>
              <select value={steward} onChange={(e) => setSteward(e.target.value)} aria-label="Lead agent" className={field}>
                <option value="">None</option>
                {directory.agents.map((a) => (
                  <option key={a.slug} value={a.slug}>
                    {a.name || displayAgent(a.slug)}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-muted-foreground">Lead</span>
              <select value={owner} onChange={(e) => setOwner(e.target.value)} aria-label="Lead" className={field}>
                <option value="">None</option>
                {directory.people.map((m) => (
                  <option key={m.user_id} value={m.email}>
                    {m.email}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-muted-foreground">Target date</span>
              <input type="date" value={target} onChange={(e) => setTarget(e.target.value)} aria-label="Target date" className={field} />
            </label>
          </div>
          {error && <p className="text-[12px] text-destructive">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" size="sm" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" size="sm" disabled={busy || !name.trim()}>
              Create project
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}
