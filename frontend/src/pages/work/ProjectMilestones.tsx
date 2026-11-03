import { errorText } from '@/api/problem'
import { DateField } from '@/pages/work/ProjectProperties'
import { useState, type JSX } from 'react'
import { Pencil, Trash2 } from 'lucide-react'
import { Button } from 'canopy-ui/ui'
import { createMilestone, deleteMilestone, updateMilestone, type Milestone } from '@/api/work'
import { Section } from '@/components/objectPage/settingsLayout'
import { ProgressBar } from '@/components/work/projectBits'
import { Td, Th, Tr } from '@/components/DataTable'

export function MilestonesSection({
  milestones,
  editable,
  workspace,
  handle,
  selected,
  onSelect,
  onChanged,
}: {
  milestones: readonly Milestone[]
  editable: boolean
  workspace: string
  handle: string
  selected: string | null
  onSelect: (name: string | null) => void
  onChanged: () => void
}): JSX.Element {
  const [error, setError] = useState<string | null>(null)
  const run = (p: Promise<unknown>) => {
    setError(null)
    p.then(onChanged).catch((err: unknown) => setError(errorText(err, 'Could not save the milestone')))
  }
  return (
    <Section id="project-milestones" title="Milestones" description="The stops on the way. Pick one to see only its tasks below.">
      {milestones.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">No milestones.</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border bg-card" data-testid="milestone-table">
          <table className="w-full border-collapse text-[13px]">
            <thead className="border-b border-border">
              <Tr head="label">
                <Th density="snug">Milestone</Th>
                <Th density="tight">Progress</Th>
                <Th density="tight">Target</Th>
                {editable && <Th density="tight" weight="inherit" className="w-16" aria-label="Actions" />}
              </Tr>
            </thead>
            <tbody>
              {milestones.map((m) => (
                <MilestoneRow
                  key={m.id}
                  milestone={m}
                  editable={editable}
                  selected={selected === m.name}
                  onSelect={() => onSelect(selected === m.name ? null : m.name)}
                  onRename={(name) => run(updateMilestone(workspace, handle, m.id, { name }))}
                  onDate={(target_date) => run(updateMilestone(workspace, handle, m.id, { target_date }))}
                  onDelete={() => {
                    if (selected === m.name) onSelect(null)
                    run(deleteMilestone(workspace, handle, m.id))
                  }}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {error && <p className="mt-2 text-[12px] text-destructive">{error}</p>}
      {editable && <AddMilestone onAdd={(name, target_date) => run(createMilestone(workspace, handle, { name, target_date }))} />}
    </Section>
  )
}

function MilestoneRow({
  milestone: m,
  editable,
  selected,
  onSelect,
  onRename,
  onDate,
  onDelete,
}: {
  milestone: Milestone
  editable: boolean
  selected: boolean
  onSelect: () => void
  onRename: (name: string) => void
  onDate: (d: string | null) => void
  onDelete: () => void
}): JSX.Element {
  const [renaming, setRenaming] = useState(false)
  const [draft, setDraft] = useState(m.name)
  return (
    <Tr rule="bottom-last" className={`hover:bg-muted/50 ${selected ? 'bg-primary/5' : ''}`} data-milestone={m.name}>
      <Td density="snug">
        {renaming ? (
          <input
            autoFocus
            value={draft}
            aria-label="Milestone name"
            onChange={(e) => setDraft(e.target.value)}
            onBlur={() => {
              setRenaming(false)
              if (draft.trim() && draft.trim() !== m.name) onRename(draft.trim())
              else setDraft(m.name)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
              if (e.key === 'Escape') {
                setDraft(m.name)
                setRenaming(false)
              }
            }}
            className="w-full rounded border border-input bg-input px-1 text-[13px] text-foreground"
          />
        ) : (
          <button type="button" onClick={onSelect} aria-pressed={selected} className="text-left font-medium text-foreground hover:text-primary">
            {m.name}
          </button>
        )}
        {m.description && <div className="text-[12px] text-muted-foreground">{m.description}</div>}
      </Td>
      <Td density="tight">
        <ProgressBar done={m.done} total={m.total} />
      </Td>
      <Td density="tight">
        <DateField label={`${m.name} target date`} value={m.target_date} disabled={!editable} onChange={onDate} />
      </Td>
      {editable && (
        <Td density="tight" align="right">
          <span className="inline-flex gap-1">
            <button type="button" aria-label={`Rename ${m.name}`} onClick={() => setRenaming(true)} className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground">
              <Pencil aria-hidden="true" className="size-3.5" />
            </button>
            <button type="button" aria-label={`Delete ${m.name}`} onClick={onDelete} className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-destructive">
              <Trash2 aria-hidden="true" className="size-3.5" />
            </button>
          </span>
        </Td>
      )}
    </Tr>
  )
}

function AddMilestone({ onAdd }: { onAdd: (name: string, target: string | null) => void }): JSX.Element {
  const [name, setName] = useState('')
  const [target, setTarget] = useState('')
  return (
    <form
      className="mt-2 flex flex-wrap gap-2"
      data-testid="project-milestones-form"
      onSubmit={(e) => {
        e.preventDefault()
        if (!name.trim()) return
        onAdd(name.trim(), target || null)
        setName('')
        setTarget('')
      }}
    >
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="Add a milestone…"
        aria-label="New milestone"
        className="min-w-0 flex-1 rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground placeholder:text-placeholder"
      />
      <input
        type="date"
        value={target}
        onChange={(e) => setTarget(e.target.value)}
        aria-label="New milestone target date"
        className="rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground"
      />
      <Button size="sm" variant="outline" type="submit" disabled={!name.trim()}>
        Add
      </Button>
    </form>
  )
}
