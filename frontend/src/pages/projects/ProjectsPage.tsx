import { useEffect, useMemo, useState, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'

import { listFleetProjects, listWorkspaceProjects, type ProjectOut } from '@/api/agents'
import { relativeTime } from '@/components/activity/turnLog'

// EVERY project in the workspace, across its agents — the list the header's
// Projects ▸ All projects opens. A project's one page is on its agent's board
// (`url`); this is only the index of them, so it renders rows, never a second
// project page. Agent projects and workspace projects are one list: a workspace
// project is an agent project with a handle and a steward.

const STATUS_ORDER: ProjectOut['status'][] = ['active', 'proposed', 'done', 'archived']

type Filter = 'live' | 'all'

export function ProjectsPage(): JSX.Element {
  const { workspace = '' } = useParams()
  const [projects, setProjects] = useState<ProjectOut[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<Filter>('live')
  const [q, setQ] = useState('')

  useEffect(() => {
    let cancelled = false
    // The fleet list finds a project through its agent; a workspace project kept
    // by an agent of a parent workspace is only in the workspace-projects list.
    Promise.all([listFleetProjects(''), listWorkspaceProjects(workspace).catch(() => [] as ProjectOut[])])
      .then(([fleet, shared]) => {
        if (cancelled) return
        const byId = new Map(fleet.map((p) => [p.id, p]))
        for (const p of shared) if (p.workspace === workspace) byId.set(p.id, p)
        setProjects([...byId.values()])
      })
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : 'Could not load projects'))
    return () => {
      cancelled = true
    }
  }, [workspace])

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return (projects ?? [])
      .filter((p) => filter === 'all' || p.status === 'active' || p.status === 'proposed')
      .filter((p) => !needle || `${p.name} ${p.outcome} ${p.agent_slug} ${p.handle}`.toLowerCase().includes(needle))
      .sort(
        (a, b) =>
          STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status) ||
          b.waiting_task_count - a.waiting_task_count ||
          Date.parse(b.updated_at) - Date.parse(a.updated_at),
      )
  }, [projects, filter, q])

  const hidden = (projects?.length ?? 0) - (projects ?? []).filter((p) => p.status === 'active' || p.status === 'proposed').length

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-foreground">Projects</h1>
          <p className="mt-1 text-[13px] text-muted-foreground">
            Every project the agents in this workspace are working on. A project opens on its agent's board.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Filter projects"
            aria-label="Filter projects"
            className="rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground"
          />
          <div className="flex rounded border border-border text-[12px]">
            {(['live', 'all'] as const).map((f) => (
              <button
                key={f}
                type="button"
                onClick={() => setFilter(f)}
                className={`px-2 py-1 ${filter === f ? 'bg-card text-foreground font-medium' : 'text-muted-foreground hover:text-foreground'}`}
              >
                {f === 'live' ? 'Active' : 'All'}
              </button>
            ))}
          </div>
        </div>
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}
      {!projects && !error && <p className="text-sm text-muted-foreground">Loading…</p>}

      {projects && rows.length === 0 && (
        <p className="rounded border border-border bg-card p-4 text-[13px] text-foreground-secondary">
          {projects.length === 0
            ? 'No projects yet. An agent starts one from its board (Agents ▸ an agent ▸ Projects ▸ Add project).'
            : 'No project matches.'}
        </p>
      )}

      {rows.length > 0 && (
        <div className="overflow-x-auto rounded border border-border">
          <table className="w-full text-[13px]">
            <thead className="bg-card text-left text-[11px] uppercase tracking-wide text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Project</th>
                <th className="px-3 py-2 font-medium">Agent</th>
                <th className="px-3 py-2 font-medium">Status</th>
                <th className="px-3 py-2 text-right font-medium">Open</th>
                <th className="px-3 py-2 text-right font-medium">Waiting</th>
                <th className="px-3 py-2 font-medium">Owner</th>
                <th className="px-3 py-2 font-medium">Updated</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => (
                <tr key={p.id} className="border-t border-border align-top hover:bg-card/60">
                  <td className="px-3 py-2">
                    <Link to={p.board_url || p.url} className="font-medium text-foreground hover:text-primary">
                      {p.name}
                    </Link>
                    {p.scope === 'workspace' && (
                      <span className="ml-1.5 rounded bg-muted px-1 py-px text-[10px] text-muted-foreground" title="Owned by the workspace, kept current by its steward">
                        workspace · {p.address || p.handle}
                      </span>
                    )}
                    {p.outcome && <div className="line-clamp-1 text-[12px] text-muted-foreground">{p.outcome}</div>}
                  </td>
                  <td className="px-3 py-2">
                    {p.scope === 'workspace' ? (
                      // A steward may live in a parent workspace: its board is the project's link.
                      <span className="text-foreground-secondary">{p.steward_slug || p.agent_slug}</span>
                    ) : (
                      <Link to={`/w/${workspace}/agents/${p.agent_slug}`} className="text-foreground-secondary hover:text-primary">
                        {p.agent_slug}
                      </Link>
                    )}
                  </td>
                  <td className="px-3 py-2 capitalize text-foreground-secondary">{p.status}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{p.open_task_count}</td>
                  <td className={`px-3 py-2 text-right tabular-nums ${p.waiting_task_count ? 'font-medium text-warning' : 'text-muted-foreground'}`}>
                    {p.waiting_task_count}
                  </td>
                  <td className="px-3 py-2 text-foreground-secondary">{p.owner_email ?? '—'}</td>
                  <td className="px-3 py-2 text-muted-foreground">{relativeTime(p.updated_at, new Date())}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {filter === 'live' && hidden > 0 && (
        <button type="button" onClick={() => setFilter('all')} className="mt-2 text-[12px] text-muted-foreground hover:text-foreground">
          {hidden} done or archived — show all
        </button>
      )}
    </div>
  )
}
