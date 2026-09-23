import { useState, type JSX } from 'react'

import {
  createAgentProject,
  patchAgentProject,
  type AgentProjectOut,
  type AgentTaskOut,
} from '@/api/agents'

// What a PROJECT knows that a task cannot: how much is open, what is waiting on
// a person, and where the Drive folder is. canopy keeps the state; Drive keeps
// the files, per `agent-core/deliverables.md`.
//
// These were the Projects page. That page listed the same tasks the board
// listed, so it is a GROUPING of Work now rather than a destination — and this
// is the part of it worth keeping: the header of each group.

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

export function ProjectGroupHeader({
  project,
  tasks,
  slug,
  onChanged,
}: {
  project: AgentProjectOut
  tasks: AgentTaskOut[]
  slug: string
  onChanged: () => void
}): JSX.Element {
  // Kept from the Projects page, and it is no longer the only place you can see
  // it: the cards below now carry their own ask, so this is a count rather than
  // the sole signal.
  const waiting = tasks.filter((t) => t.status === 'suggested' || t.ask_state === 'open').length

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
              void patchAgentProject(slug, project.ext_id, { status: 'done' }).then(onChanged)
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

export function NewProject({ slug, onCreated }: { slug: string; onCreated: () => void }): JSX.Element {
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
        className="w-72 rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground"
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
