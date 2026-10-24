// The bulk bar: one change to every selected task — a status, a priority, an
// assignee, a label added, a project, or a decision (Accept / Backlog / Done /
// Decline) — through `bulkTasks`, which answers per task. What the server
// refused stays listed here, key by key, so a partial success is never
// mistaken for a whole one.
import { useState, type JSX, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { Button } from 'canopy-ui/ui'

import { bulkTasks, type BulkResult, type Task, type TaskActionName, type TaskPatch } from '@/api/work'

import { PRIORITIES, STATUS_ORDER, displayAgent, statusName } from './meta'
import type { Directory } from './useDirectory'

const SELECT =
  'h-7 rounded border border-input bg-input px-1.5 text-[12px] text-foreground disabled:opacity-60'

const ACTIONS: { action: TaskActionName; label: string }[] = [
  { action: 'accept', label: 'Accept' },
  { action: 'backlog', label: 'Backlog' },
  { action: 'done', label: 'Done' },
  { action: 'decline', label: 'Decline' },
]

export function BulkBar({
  tasks,
  directory,
  names,
  onApplied,
  onClear,
}: {
  /** The selected tasks. */
  tasks: readonly Task[]
  directory: Directory
  names?: Record<string, string> | null
  /** Every answer, changed or not; the caller takes the changed rows in. */
  onApplied: (results: BulkResult[]) => void
  onClear: () => void
}): JSX.Element | null {
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<BulkResult[]>([])
  const keys = tasks.map((t) => t.key)
  if (keys.length === 0) return null

  const run = async (batches: { keys: string[]; change: Parameters<typeof bulkTasks>[1] }[]) => {
    setBusy(true)
    setFailed([])
    try {
      const results = (await Promise.all(batches.map((b) => bulkTasks(b.keys, b.change)))).flat()
      setFailed(results.filter((r) => !r.ok))
      onApplied(results)
    } catch (e) {
      setFailed(keys.map((key) => ({ key, ok: false, error: e instanceof Error ? e.message : 'Not changed' }) as BulkResult))
    } finally {
      setBusy(false)
    }
  }
  const patch = (body: TaskPatch) => run([{ keys, change: { patch: body } }])

  // Labels REPLACE on a PATCH, so "add a label" is one batch per resulting set.
  const addLabel = (name: string) => {
    const groups = new Map<string, { names: string[]; keys: string[] }>()
    for (const t of tasks) {
      const have = (t.labels ?? []).map((l) => l.name)
      const next = have.some((n) => n.toLowerCase() === name.toLowerCase()) ? have : [...have, name]
      const id = next.join('\u0000')
      const g = groups.get(id) ?? { names: next, keys: [] }
      g.keys.push(t.key)
      groups.set(id, g)
    }
    return run([...groups.values()].map((g) => ({ keys: g.keys, change: { patch: { labels: g.names } } })))
  }

  return (
    <div className="sticky bottom-3 z-30 mt-3 rounded-lg border border-border bg-card px-3 py-2 text-[12px] shadow-lg" role="region" aria-label="Change selected tasks">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-foreground">{keys.length} selected</span>
        <Choose label="Set status" disabled={busy} onPick={(v) => patch({ status: v })}>
          {STATUS_ORDER.map((s) => (
            <option key={s} value={s}>
              {statusName(s, names)}
            </option>
          ))}
        </Choose>
        <Choose label="Set priority" disabled={busy} onPick={(v) => patch({ priority: Number(v) })}>
          {PRIORITIES.map((p) => (
            <option key={p.value} value={String(p.value)}>
              {p.label}
            </option>
          ))}
        </Choose>
        <Choose label="Assign to" disabled={busy} onPick={(v) => patch({ assignee: v === '-' ? '' : v })}>
          <option value="-">Nobody</option>
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
        </Choose>
        <Choose label="Add label" disabled={busy || directory.labels.length === 0} onPick={addLabel}>
          {directory.labels.map((l) => (
            <option key={l.id} value={l.name}>
              {l.name}
            </option>
          ))}
        </Choose>
        <Choose label="Move to project" disabled={busy} onPick={(v) => patch({ project: v === '-' ? '' : v })}>
          <option value="-">No project</option>
          {directory.projects.map((p) => (
            <option key={p.id} value={p.address}>
              {p.name}
            </option>
          ))}
        </Choose>
        <span className="mx-1 h-4 w-px bg-border" aria-hidden="true" />
        {ACTIONS.map((a) => (
          <Button key={a.action} size="xs" variant="outline" disabled={busy} onClick={() => run([{ keys, change: { action: { action: a.action } } }])}>
            {a.label}
          </Button>
        ))}
        <button type="button" onClick={onClear} aria-label="Clear selection" className="ml-auto rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground">
          <X className="size-3.5" aria-hidden="true" />
        </button>
      </div>
      {failed.length > 0 && (
        <div role="alert" className="mt-1.5 text-destructive">
          {failed.length} not changed:{' '}
          {failed.map((f, i) => (
            <span key={f.key}>
              {i > 0 && '; '}
              <span className="font-mono">{f.key}</span> {f.error || 'refused'}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

/** A one-shot menu: pick a value and it is applied; the select resets. */
function Choose({
  label,
  disabled,
  onPick,
  children,
}: {
  label: string
  disabled?: boolean
  onPick: (value: string) => void
  children: ReactNode
}): JSX.Element {
  return (
    <select
      aria-label={label}
      value=""
      disabled={disabled}
      onChange={(e) => e.target.value && onPick(e.target.value)}
      className={SELECT}
    >
      <option value="">{label}…</option>
      {children}
    </select>
  )
}
