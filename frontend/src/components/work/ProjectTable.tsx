// THE project row — one dense table, the same on the workspace's Projects page
// and an agent's Projects tab: name, status, health (or "Update missing"),
// lead person and lead agent, the other agents, progress, open and waiting
// counts, target date, last change. A row's name opens the project's one page
// (`url`). The filter bar beside it (status, text, sort) is shared too.
import type { JSX, ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { cn } from 'canopy-ui/lib'

import type { Project } from '@/api/work'
import { relativeAge } from '@/lib/relativeAge'

import { PROJECT_STATUSES } from './meta'
import { AgentList, AgentName, HealthBadge, PersonName, ProgressBar, ProjectStatusBadge } from './projectBits'
import { Td, Th, Tr } from '@/components/DataTable'

/** Still in play: what "Open" means on the Projects page (the server's `status=open`). */
export const OPEN_PROJECT_STATUSES: readonly Project['status'][] = ['proposed', 'planned', 'active', 'paused']

export type ProjectStatusFilter = 'open' | 'all' | Project['status']
export type ProjectSort = 'updated' | 'target' | 'name'

/** The `status=` the projects list takes for a filter. */
export function statusQuery(filter: ProjectStatusFilter): string {
  return filter === 'all' ? '' : filter
}

export function filterProjects(
  rows: readonly Project[],
  { status, q, sort }: { status: ProjectStatusFilter; q: string; sort: ProjectSort },
): Project[] {
  const needle = q.trim().toLowerCase()
  const out = rows
    .filter((p) => (status === 'all' ? true : status === 'open' ? OPEN_PROJECT_STATUSES.includes(p.status) : p.status === status))
    .filter(
      (p) =>
        !needle ||
        `${p.name} ${p.outcome} ${p.handle} ${p.agent_slug ?? ''} ${p.owner_email ?? ''} ${(p.member_agents ?? []).join(' ')}`
          .toLowerCase()
          .includes(needle),
    )
  const byUpdated = (a: Project, b: Project) => Date.parse(b.updated_at) - Date.parse(a.updated_at)
  if (sort === 'name') out.sort((a, b) => a.name.localeCompare(b.name))
  else if (sort === 'target')
    // Soonest target first; projects with no target date last.
    out.sort((a, b) =>
      a.target_date && b.target_date
        ? a.target_date.localeCompare(b.target_date)
        : a.target_date
          ? -1
          : b.target_date
            ? 1
            : byUpdated(a, b),
    )
  else out.sort(byUpdated)
  return out
}

export function ProjectFilters({
  status,
  onStatus,
  q,
  onQ,
  sort,
  onSort,
  children,
}: {
  status: ProjectStatusFilter
  onStatus: (s: ProjectStatusFilter) => void
  q: string
  onQ: (q: string) => void
  sort: ProjectSort
  onSort: (s: ProjectSort) => void
  /** Actions at the end of the bar ("New project"). */
  children?: ReactNode
}): JSX.Element {
  const quick: ProjectStatusFilter[] = ['open', 'all']
  return (
    <div className="mb-3 flex flex-wrap items-center gap-2 text-[12px]" data-testid="project-filters">
      <div className="flex rounded border border-border" role="group" aria-label="Show">
        {quick.map((f) => (
          <button
            key={f}
            type="button"
            aria-pressed={status === f}
            onClick={() => onStatus(f)}
            className={cn('px-2 py-1', status === f ? 'bg-card font-medium text-foreground' : 'text-muted-foreground hover:text-foreground')}
          >
            {f === 'open' ? 'Open' : 'All'}
          </button>
        ))}
      </div>
      <select
        aria-label="Status"
        value={quick.includes(status) ? '' : status}
        onChange={(e) => onStatus((e.target.value || 'open') as ProjectStatusFilter)}
        className="rounded border border-input bg-input px-1.5 py-1 text-[12px] text-foreground"
      >
        <option value="">Any status</option>
        {PROJECT_STATUSES.map((s) => (
          <option key={s.value} value={s.value}>
            {s.label}
          </option>
        ))}
      </select>
      <input
        type="search"
        value={q}
        onChange={(e) => onQ(e.target.value)}
        placeholder="Filter projects"
        aria-label="Filter projects"
        className="min-w-0 rounded border border-input bg-input px-2 py-1 text-[12px] text-foreground placeholder:text-placeholder"
      />
      <label className="inline-flex items-center gap-1 text-muted-foreground">
        Sort
        <select
          aria-label="Sort"
          value={sort}
          onChange={(e) => onSort(e.target.value as ProjectSort)}
          className="rounded border border-input bg-input px-1.5 py-1 text-[12px] text-foreground"
        >
          <option value="updated">Last updated</option>
          <option value="target">Target date</option>
          <option value="name">Name</option>
        </select>
      </label>
      {children && <span className="ml-auto flex items-center gap-2">{children}</span>}
    </div>
  )
}

function shortDate(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(`${iso}T00:00:00`)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

export function ProjectTable({
  projects,
  empty,
  showLeadAgent = true,
}: {
  projects: readonly Project[]
  empty?: ReactNode
  /** An agent's own tab already says whose projects these are. */
  showLeadAgent?: boolean
}): JSX.Element {
  if (projects.length === 0) {
    return <div className="rounded-lg border border-border bg-card px-4 py-6 text-[13px] text-muted-foreground">{empty ?? 'No projects.'}</div>
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-border bg-card" data-testid="project-table">
      <table className="w-full border-collapse text-[13px]">
        <thead className="border-b border-border">
          <tr>
            <Th density="tight" align="left" label className="pl-3">Project</Th>
            <Th density="tight" align="left" label>Status</Th>
            <Th density="tight" align="left" label>Health</Th>
            <Th density="tight" align="left" label hideBelow="md">Lead</Th>
            {showLeadAgent && <Th density="tight" align="left" label hideBelow="md">Lead agent</Th>}
            <Th density="tight" align="left" label hideBelow="lg">Agents</Th>
            <Th density="tight" align="left" label>Progress</Th>
            <Th density="tight" align="right" label>Open</Th>
            <Th density="tight" align="right" label>Waiting</Th>
            <Th density="tight" align="left" label hideBelow="sm">Target</Th>
            <Th density="tight" align="right" label hideBelow="md" className="pr-3">Updated</Th>
          </tr>
        </thead>
        <tbody>
          {projects.map((p) => {
            const others = (p.member_agents ?? []).filter((s) => s !== p.agent_slug)
            return (
              <Tr rule="bottom-last" key={p.id} data-project={p.address || p.id} className="hover:bg-muted/50">
                <Td density="cozy-gutter" className="max-w-[22rem] pl-3">
                  <Link to={p.url} className="block truncate font-medium text-foreground hover:text-primary">
                    {p.name}
                  </Link>
                  {p.outcome && <div className="truncate text-[12px] text-muted-foreground">{p.outcome}</div>}
                </Td>
                <Td density="tight">
                  <ProjectStatusBadge status={p.status} />
                </Td>
                <Td density="tight">
                  <HealthBadge project={p} /> {!p.update_missing && !p.health && <span className="text-foreground-subtle">—</span>}
                </Td>
                <Td density="tight" hideBelow="md" className="max-w-[9rem]">
                  {p.owner_email ? <PersonName email={p.owner_email} /> : <span className="text-foreground-subtle">—</span>}
                </Td>
                {showLeadAgent && (
                  <Td density="tight" hideBelow="md" className="max-w-[9rem]">
                    {p.agent_slug ? <AgentName slug={p.agent_slug} /> : <span className="text-foreground-subtle">—</span>}
                  </Td>
                )}
                <Td density="tight" hideBelow="lg" className="max-w-[10rem]">
                  {others.length ? <AgentList slugs={others} max={2} /> : <span className="text-foreground-subtle">—</span>}
                </Td>
                <Td density="tight">
                  <ProgressBar done={p.done_task_count} total={p.task_count} />
                </Td>
                <Td density="tight" numeric align="right">{p.open_task_count}</Td>
                <Td
                  density="tight"
                  align="right"
                  numeric
                  className={p.waiting_task_count ? 'font-medium text-warning' : 'text-muted-foreground'}
                >
                  {p.waiting_task_count}
                </Td>
                <Td density="tight" tone="secondary" nowrap hideBelow="sm" className="text-[12px]">
                  {shortDate(p.target_date) || <span className="text-foreground-subtle">—</span>}
                </Td>
                <Td density="cozy" tone="muted" nowrap align="right" hideBelow="md"
                  className="pl-2 pr-3 text-[11.5px]"
                  title={p.updated_at}
                >
                  {relativeAge(p.updated_at, new Date(), { seconds: false })}
                </Td>
              </Tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
