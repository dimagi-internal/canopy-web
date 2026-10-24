// New task: a title and the few properties worth setting at birth (project,
// assignee, priority, status, due). Filed in the list's workspace; the new
// task opens beside the list, where everything else about it is edited. The
// caller re-keys it per opening, so each starts from the list's defaults.
import { useState, type FormEvent, type JSX } from 'react'
import { Button, Dialog, DialogContent, DialogFooter, DialogTitle, Input } from 'canopy-ui/ui'

import { createTask, type Task, type TaskStatus } from '@/api/work'

import { PRIORITIES, STATUS_ORDER, displayAgent, statusName } from './meta'
import type { Directory } from './useDirectory'

const FIELD = 'h-8 w-full rounded border border-input bg-input px-2 text-[13px] text-foreground'

export interface NewTaskDefaults {
  /** A project address (`<ws>/<handle>`). */
  project?: string
  /** `agent:<slug>` or an email. */
  assignee?: string
}

export function NewTaskDialog({
  open,
  onOpenChange,
  workspace,
  directory,
  defaults,
  names,
  onCreated,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  workspace: string
  directory: Directory
  defaults?: NewTaskDefaults
  names?: Record<string, string> | null
  onCreated: (task: Task) => void
}): JSX.Element {
  const [title, setTitle] = useState('')
  const [project, setProject] = useState(defaults?.project ?? '')
  const [assignee, setAssignee] = useState(defaults?.assignee ?? '')
  const [priority, setPriority] = useState(0)
  const [status, setStatus] = useState<TaskStatus>('todo')
  const [due, setDue] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!title.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      const task = await createTask({
        title: title.trim(),
        workspace,
        priority,
        status,
        ...(project ? { project } : {}),
        ...(assignee ? { assignee } : {}),
        ...(due ? { due } : {}),
      })
      onOpenChange(false)
      onCreated(task)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create the task')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o: boolean) => onOpenChange(o)}>
      <DialogContent className="w-[calc(100%-2rem)] max-w-lg bg-card">
        <DialogTitle className="text-sm font-semibold text-foreground">New task</DialogTitle>
        <form onSubmit={submit} className="grid gap-3 text-[13px]">
          <Input
            autoFocus
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Task title"
            aria-label="Title"
          />
          <div className="grid grid-cols-2 gap-2">
            <label className="grid gap-1 text-[12px] text-muted-foreground">
              Project
              <select value={project} onChange={(e) => setProject(e.target.value)} className={FIELD}>
                <option value="">No project</option>
                {directory.projects.map((p) => (
                  <option key={p.id} value={p.address}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="grid gap-1 text-[12px] text-muted-foreground">
              Assignee
              <select value={assignee} onChange={(e) => setAssignee(e.target.value)} className={FIELD}>
                <option value="">Nobody</option>
                {directory.agents.map((a) => (
                  <option key={a.slug} value={`agent:${a.slug}`}>
                    {a.name || displayAgent(a.slug)}
                  </option>
                ))}
                {directory.people.map((m) => (
                  <option key={m.user_id} value={m.email}>
                    {m.email}
                  </option>
                ))}
              </select>
            </label>
            <label className="grid gap-1 text-[12px] text-muted-foreground">
              Priority
              <select value={priority} onChange={(e) => setPriority(Number(e.target.value))} className={FIELD}>
                {PRIORITIES.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="grid gap-1 text-[12px] text-muted-foreground">
              Status
              <select value={status} onChange={(e) => setStatus(e.target.value as TaskStatus)} className={FIELD}>
                {STATUS_ORDER.map((s) => (
                  <option key={s} value={s}>
                    {statusName(s, names)}
                  </option>
                ))}
              </select>
            </label>
            <label className="grid gap-1 text-[12px] text-muted-foreground">
              Due
              <input type="date" value={due} onChange={(e) => setDue(e.target.value)} className={FIELD} />
            </label>
          </div>
          {error && (
            <p role="alert" className="text-[12px] text-destructive">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" size="sm" disabled={!title.trim() || busy}>
              {busy ? 'Creating…' : 'Create task'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
