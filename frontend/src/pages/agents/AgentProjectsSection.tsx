import { useCallback, useEffect, useState, type JSX } from 'react'
import { useOutletContext } from 'react-router-dom'

import {
  createAgentProject,
  listAgentProjects,
  listAgentTasks,
  patchAgentProject,
  type AgentProjectOut,
  type AgentTaskOut,
} from '@/api/agents'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { WorkbenchSkeleton, WorkbenchSubHeader } from 'canopy-ui'

/**
 * An agent's projects — the work each `Projects/<name>` Drive folder holds.
 *
 * canopy keeps the state (status, what is in flight, who is waiting); Drive
 * keeps the files, per agent, exactly as `agent-core/deliverables.md` lays out.
 * So a project row's job is to answer what a folder cannot: how much is open,
 * what is waiting on a person, and where the folder is.
 */

const STATUS_LABEL: Record<string, string> = {
  active: 'Active',
  done: 'Done',
  archived: 'Archived',
}

function StatusChip({ status }: { status: string }): JSX.Element {
  const tone =
    status === 'done'
      ? 'bg-success/10 text-success border-success/30'
      : status === 'archived'
        ? 'bg-muted text-muted-foreground border-border'
        : 'bg-primary/10 text-primary border-primary/30'
  return (
    <span className={`rounded border px-1.5 py-0.5 text-[10px] font-medium ${tone}`}>
      {STATUS_LABEL[status] ?? status}
    </span>
  )
}

function ProjectRow({
  project,
  tasks,
  onChanged,
}: {
  project: AgentProjectOut
  tasks: AgentTaskOut[]
  onChanged: () => void
  }): JSX.Element {
  const { agent } = useOutletContext<AgentOutletContext>()
  const mine = tasks.filter((t) => t.project_ext_id === project.ext_id)
  // "Waiting on a person" is the number the board cannot show you: a card
  // parked on a human looks identical to one the agent is working.
  const waiting = mine.filter((t) => t.status === 'suggested' || t.ask_state === 'open').length

  return (
    <div data-testid={`project-${project.ext_id}`} className="bg-card border border-border rounded-lg p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-muted-foreground">{project.ext_id}</span>
            <h3 className="text-sm font-medium text-foreground truncate">{project.name}</h3>
            <StatusChip status={project.status} />
          </div>
          {project.outcome && (
            <p className="mt-1 text-[13px] text-foreground-secondary">{project.outcome}</p>
          )}
        </div>
        {project.status === 'active' && (
          <button
            className="shrink-0 text-[11px] text-muted-foreground hover:text-foreground"
            onClick={() => {
              void patchAgentProject(agent.slug, project.ext_id, { status: 'done' }).then(onChanged)
            }}
          >
            Mark done
          </button>
        )}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-3 text-[11px] text-muted-foreground">
        <span>
          {project.open_task_count} open / {project.task_count} task
          {project.task_count === 1 ? '' : 's'}
        </span>
        {waiting > 0 && <span className="text-primary">{waiting} waiting on a person</span>}
        {project.drive_folder_url && (
          <a
            href={project.drive_folder_url}
            target="_blank"
            rel="noreferrer"
            className="text-primary hover:underline"
          >
            Drive folder
          </a>
        )}
        {project.repo_slug && <span>repo: {project.repo_slug}</span>}
      </div>

      {mine.length > 0 && (
        <ul className="mt-3 space-y-1 border-t border-border pt-2">
          {mine.map((task) => (
            <li key={task.id} className="flex items-baseline gap-2 text-[13px]">
              <span className="text-[11px] text-muted-foreground shrink-0">{task.ext_id}</span>
              <span className="text-foreground-secondary truncate">{task.title}</span>
              <span className="ml-auto text-[11px] text-muted-foreground shrink-0">
                {task.status.replace('_', ' ')}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function NewProject({ slug, onCreated }: { slug: string; onCreated: () => void }): JSX.Element {
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)

  return (
    <form
      className="flex items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault()
        const trimmed = name.trim()
        if (!trimmed || busy) return
        setBusy(true)
        void createAgentProject(slug, { name: trimmed })
          .then(() => {
            setName('')
            onCreated()
          })
          .finally(() => setBusy(false))
      }}
    >
      <input
        aria-label="New project name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="e.g. UNGA 2026 conference planning"
        className="bg-input border border-input rounded px-2 py-1 text-[13px] text-foreground w-72"
      />
      <button
        type="submit"
        disabled={busy || !name.trim()}
        className="rounded bg-primary px-2 py-1 text-[12px] text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
      >
        Add project
      </button>
    </form>
  )
}

export function AgentProjectsSection(): JSX.Element {
  const { agent } = useOutletContext<AgentOutletContext>()
  const [projects, setProjects] = useState<AgentProjectOut[] | null>(null)
  const [tasks, setTasks] = useState<AgentTaskOut[]>([])

  const reload = useCallback(() => {
    void listAgentProjects(agent.slug)
      .then(setProjects)
      .catch(() => setProjects([]))
    void listAgentTasks(agent.slug)
      .then(setTasks)
      .catch(() => setTasks([]))
  }, [agent.slug])

  useEffect(() => {
    setProjects(null)
    reload()
  }, [reload])

  const unfiled = tasks.filter((t) => !t.project_ext_id)

  return (
    <div className="max-w-4xl px-6 py-8">
      <WorkbenchSubHeader
        title="Projects"
        count={projects?.length}
        action={<NewProject slug={agent.slug} onCreated={reload} />}
      />
      {projects === null ? (
        <WorkbenchSkeleton />
      ) : projects.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">
          No projects yet. A project is the work one of {agent.name}&apos;s{' '}
          <code className="text-foreground-secondary">Projects/&lt;name&gt;</code> Drive folders
          holds — add one and file its tasks into it.
        </p>
      ) : (
        <div className="space-y-3">
          {projects.map((project) => (
            <ProjectRow key={project.id} project={project} tasks={tasks} onChanged={reload} />
          ))}
        </div>
      )}

      {unfiled.length > 0 && (
        <p className="mt-4 text-[11px] text-muted-foreground">
          {unfiled.length} task{unfiled.length === 1 ? '' : 's'} not in a project — one-offs are
          fine; a project per task is not.
        </p>
      )}
    </div>
  )
}
