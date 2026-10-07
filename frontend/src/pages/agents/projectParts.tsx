import { useState, type JSX } from 'react'

import { createProject, patchProject, type ProjectOut, type TaskOut } from '@/api/agents'

// What a PROJECT knows that a task cannot: how much is open, what is waiting on
// a person, and where the Drive folder is. canopy keeps the state; Drive keeps
// the files, per `agent-core/deliverables.md`.
//
// Shared by the Projects page (`AgentProjectsSection`), the project page
// (`AgentProjectPage`) and the Tasks page's group-by-project view, which uses
// `ProjectGroupHeader` as the header of each group.

const STATUS_LABEL: Record<string, string> = {
  active: 'Active',
  done: 'Done',
  archived: 'Archived',
}

/** The `<option>`s for a project-status `<select>`, labelled like the chip. */
export function ProjectStatusOptions(): JSX.Element {
  return (
    <>
      {Object.entries(STATUS_LABEL).map(([value, label]) => (
        <option key={value} value={value}>
          {label}
        </option>
      ))}
    </>
  )
}

export function StatusChip({ status }: { status: string }): JSX.Element {
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

export function ProjectGroupHeader({
  project,
  tasks,
  slug,
  onChanged,
}: {
  project: ProjectOut
  tasks: TaskOut[]
  slug: string
  onChanged: () => void
}): JSX.Element {
  // The cards below carry their own ask, so this is a count rather than the
  // sole signal.
  const waiting = tasks.filter((t) => t.status === 'suggested' || t.ask_open).length

  return (
    <div className="border-b border-border pb-2">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-muted-foreground">{project.ext_id}</span>
            <h3 className="truncate text-sm font-medium text-foreground">{project.name}</h3>
            <StatusChip status={project.status} />
          </div>
          {project.outcome && (
            <p className="mt-1 text-[13px] text-foreground-secondary">{project.outcome}</p>
          )}
        </div>
        {project.status === 'active' && (
          <button
            type="button"
            className="shrink-0 text-[11px] text-muted-foreground hover:text-foreground"
            onClick={() => {
              void patchProject(slug, project.ext_id, { status: 'done' }).then(onChanged)
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
    </div>
  )
}

/** "Add project" takes a name AND an outcome, both required: a project without
 *  "what done looks like" is a task, and the outcome is the line every row on
 *  the Projects page leads with. The server accepts an empty outcome (the CLI
 *  and agents create projects too); the requirement is the UI's. */
export function NewProject({
  slug,
  onCreated,
  canEdit,
}: {
  slug: string
  onCreated: () => void
  /** Creating a project is an editor action (`_agent_for_write`). */
  canEdit: boolean
}): JSX.Element {
  const [name, setName] = useState('')
  const [outcome, setOutcome] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ready = name.trim() !== '' && outcome.trim() !== ''

  return (
    <form
      className="flex flex-wrap items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault()
        if (!ready || busy || !canEdit) return
        setBusy(true)
        setError(null)
        void createProject(slug, { name: name.trim(), outcome: outcome.trim() })
          .then(() => {
            setName('')
            setOutcome('')
            onCreated()
          })
          .catch((err: unknown) => setError(err instanceof Error ? err.message : 'Could not add the project'))
          .finally(() => setBusy(false))
      }}
    >
      <input
        aria-label="New project name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="Name, e.g. UNGA 2026 conference planning"
        className="min-h-11 w-full rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground sm:min-h-0 sm:w-64"
      />
      <input
        aria-label="New project outcome"
        value={outcome}
        onChange={(e) => setOutcome(e.target.value)}
        placeholder="What done looks like"
        className="min-h-11 w-full flex-1 rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground sm:min-h-0 sm:min-w-64"
      />
      <button
        type="submit"
        disabled={busy || !ready || !canEdit}
        title={canEdit ? undefined : 'Adding a project needs the editor role'}
        className="min-h-11 rounded bg-primary px-2 py-1 text-[12px] text-primary-foreground hover:bg-primary/90 disabled:opacity-50 sm:min-h-0"
      >
        Add project
      </button>
      {error && <p className="basis-full text-[12px] text-destructive">{error}</p>}
    </form>
  )
}
