import { useState, type JSX } from 'react'
import { StatusBadge } from 'canopy-ui/ui'

import { createProject } from '@/api/agents'
import { projectStatusMeta } from '@/components/work/meta'

// Two small pieces of an agent's project list that the workspace's project
// pages do not need: the status chip a proposal card shows, and the agent's own
// "Add project" (made through its route, so the project carries its `P<n>`).

export function StatusChip({ status }: { status: string }): JSX.Element {
  const meta = projectStatusMeta(status)
  return <StatusBadge tone={meta.tone}>{meta.label}</StatusBadge>
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
