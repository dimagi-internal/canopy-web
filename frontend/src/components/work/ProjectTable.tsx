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
  const th = 'px-2 py-1.5 text-left text-[11px] font-medium uppercase tracking-wide text-muted-foreground'
  return (
    <div className="overflow-x-auto rounded-lg border border-border bg-card" data-testid="project-table">
      <table className="w-full border-collapse text-[13px]">
        <thead className="border-b border-border">
          <tr>
            <th className={cn(th, 'pl-3')}>Project</th>
            <th className={th}>Status</th>
            <th className={th}>Health</th>
            <th className={cn(th, 'hidden md:table-cell')}>Lead</th>
            {showLeadAgent && <th className={cn(th, 'hidden md:table-cell')}>Lead agent</th>}
            <th className={cn(th, 'hidden lg:table-cell')}>Agents</th>
            <th className={th}>Progress</th>
            <th className={cn(th, 'text-right')}>Open</th>
            <th className={cn(th, 'text-right')}>Waiting</th>
            <th className={cn(th, 'hidden sm:table-cell')}>Target</th>
            <th className={cn(th, 'hidden pr-3 text-right md:table-cell')}>Updated</th>
          </tr>
        </thead>
        <tbody>
          {projects.map((p) => {
            const others = (p.member_agents ?? []).filter((s) => s !== p.agent_slug)
            return (
              <tr key={p.id} data-project={p.address || p.id} className="border-b border-border last:border-0 hover:bg-muted/50">
                <td className="max-w-[22rem] py-1.5 pl-3 pr-2">
                  <Link to={p.url} className="block truncate font-medium text-foreground hover:text-primary">
                    {p.name}
                  </Link>
                  {p.outcome && <div className="truncate text-[12px] text-muted-foreground">{p.outcome}</div>}
                </td>
                <td className="px-2 py-1.5">
                  <ProjectStatusBadge status={p.status} />
                </td>
                <td className="px-2 py-1.5">
                  <HealthBadge project={p} /> {!p.update_missing && !p.health && <span className="text-foreground-subtle">—</span>}
                </td>
                <td className="hidden max-w-[9rem] px-2 py-1.5 md:table-cell">
                  {p.owner_email ? <PersonName email={p.owner_email} /> : <span className="text-foreground-subtle">—</span>}
                </td>
                {showLeadAgent && (
                  <td className="hidden max-w-[9rem] px-2 py-1.5 md:table-cell">
                    {p.agent_slug ? <AgentName slug={p.agent_slug} /> : <span className="text-foreground-subtle">—</span>}
                  </td>
                )}
                <td className="hidden max-w-[10rem] px-2 py-1.5 lg:table-cell">
                  {others.length ? <AgentList slugs={others} max={2} /> : <span className="text-foreground-subtle">—</span>}
                </td>
                <td className="px-2 py-1.5">
                  <ProgressBar done={p.done_task_count} total={p.task_count} />
                </td>
                <td className="px-2 py-1.5 text-right tabular-nums">{p.open_task_count}</td>
                <td
                  className={cn(
                    'px-2 py-1.5 text-right tabular-nums',
                    p.waiting_task_count ? 'font-medium text-warning' : 'text-muted-foreground',
                  )}
                >
                  {p.waiting_task_count}
                </td>
                <td className="hidden whitespace-nowrap px-2 py-1.5 text-[12px] text-foreground-secondary sm:table-cell">
                  {shortDate(p.target_date) || <span className="text-foreground-subtle">—</span>}
                </td>
                <td
                  className="hidden whitespace-nowrap py-1.5 pl-2 pr-3 text-right text-[11.5px] text-muted-foreground md:table-cell"
                  title={p.updated_at}
                >
                  {relativeAge(p.updated_at, new Date(), { seconds: false })}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
