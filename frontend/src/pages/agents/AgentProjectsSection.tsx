// THE AGENT'S PROJECTS — its landing tab: the workspace's Projects table,
// narrowed to the projects this agent leads or works on. Two reads joined by
// id: the agent's own list (every project it leads, wherever it lives) and the
// workspace's list (the projects it is a member agent of). "Add project"
// creates through the agent, so the project carries its `P<n>` alias.
import { useCallback, useEffect, useMemo, useState, type JSX } from 'react'
import { useOutletContext, useParams } from 'react-router-dom'
import { WorkbenchSkeleton, WorkbenchSubHeader } from 'canopy-ui'

import { listProjects as listAgentProjects } from '@/api/agents'
import { listProjects, type Project } from '@/api/work'
import {
  ProjectFilters,
  ProjectTable,
  filterProjects,
  type ProjectSort,
  type ProjectStatusFilter,
} from '@/components/work/ProjectTable'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { NewProject } from '@/pages/agents/projectParts'

export function AgentProjectsSection(): JSX.Element {
  const { agent, canEdit } = useOutletContext<AgentOutletContext>()
  const { workspace = '' } = useParams()
  const [status, setStatus] = useState<ProjectStatusFilter>('open')
  const [q, setQ] = useState('')
  const [sort, setSort] = useState<ProjectSort>('updated')
  // Stamped with the slug it belongs to, so switching agents never shows the
  // previous agent's rows while the next list loads.
  const [data, setData] = useState<{ slug: string; projects: Project[]; error: string | null } | null>(null)

  const reload = useCallback(() => {
    let cancelled = false
    const slug = agent.slug
    Promise.all([
      listAgentProjects(slug) as Promise<Project[]>,
      listProjects('', workspace).catch(() => [] as Project[]),
    ])
      .then(([own, all]) => {
        if (cancelled) return
        const byId = new Map(own.map((p) => [p.id, p]))
        for (const p of all) {
          if (p.agent_slug === slug || (p.member_agents ?? []).includes(slug)) byId.set(p.id, p)
        }
        setData({ slug, projects: [...byId.values()], error: null })
      })
      .catch((err: unknown) => {
        if (!cancelled) setData({ slug, projects: [], error: err instanceof Error ? err.message : 'Could not load projects' })
      })
    return () => {
      cancelled = true
    }
  }, [agent.slug, workspace])

  useEffect(() => reload(), [reload])

  const fresh = data?.slug === agent.slug ? data : null
  const rows = useMemo(() => filterProjects(fresh?.projects ?? [], { status, q, sort }), [fresh, status, q, sort])

  return (
    <div className="px-6 py-8">
      <WorkbenchSubHeader title="Projects" count={fresh ? rows.length : undefined} />
      <div className="mb-4">
        <NewProject slug={agent.slug} canEdit={canEdit} onCreated={() => void reload()} />
      </div>
      <ProjectFilters status={status} onStatus={setStatus} q={q} onQ={setQ} sort={sort} onSort={setSort} />
      {!fresh ? (
        <WorkbenchSkeleton />
      ) : fresh.error ? (
        <p className="text-[13px] text-destructive">{fresh.error}</p>
      ) : (
        <ProjectTable
          projects={rows}
          empty={
            fresh.projects.length === 0
              ? `${agent.name || agent.slug} has no projects yet. A project is a piece of work with an end — add one above.`
              : 'No project matches.'
          }
        />
      )}
    </div>
  )
}
