// Saved views: a named set of the list's URL parameters (filters, group, sort,
// layout), yours or shared with the workspace. Picking one opens its URL;
// "Save view…" stores what the list shows now; your own can be updated to the
// current state or deleted. A shared one needs an editor — the server says so.
import { useCallback, useEffect, useState, type JSX } from 'react'
import { Button, Dialog, DialogContent, DialogFooter, DialogTitle, Input } from 'canopy-ui/ui'

import { createView, deleteView, listViews, updateView, type SavedView, type SavedViewIn } from '@/api/work'

export type ViewShape = Pick<SavedViewIn, 'filters' | 'group_by' | 'sort' | 'layout'>

export function SavedViews({
  workspace,
  current,
  activeId,
  onApply,
  onError,
}: {
  workspace: string
  /** What the list shows now, as a view would store it. */
  current: ViewShape
  /** The view the URL came from (`?saved=`). */
  activeId: string
  onApply: (view: SavedView | null) => void
  onError: (message: string) => void
}): JSX.Element {
  const [views, setViews] = useState<SavedView[]>([])
  const [saving, setSaving] = useState(false)
  const [name, setName] = useState('')
  const [shared, setShared] = useState(false)
  const [busy, setBusy] = useState(false)

  const reload = useCallback(() => {
    listViews(workspace)
      .then((rows) => setViews(rows.filter((v) => v.workspace === workspace)))
      .catch(() => setViews([]))
  }, [workspace])
  useEffect(() => reload(), [reload])

  const active = views.find((v) => String(v.id) === activeId) ?? null
  const fail = (e: unknown) => onError(e instanceof Error ? e.message : 'The view was not saved')

  const save = async () => {
    if (!name.trim()) return
    setBusy(true)
    try {
      const view = await createView({ workspace, name: name.trim(), shared, ...current })
      setSaving(false)
      setViews((vs) => [...vs, view])
      onApply(view)
    } catch (e) {
      fail(e)
    } finally {
      setBusy(false)
    }
  }

  const update = async () => {
    if (!active) return
    try {
      const view = await updateView(active.id, { ...current })
      setViews((vs) => vs.map((v) => (v.id === view.id ? view : v)))
    } catch (e) {
      fail(e)
    }
  }

  const remove = async () => {
    if (!active || !window.confirm(`Delete the view “${active.name}”?`)) return
    try {
      await deleteView(active.id)
      setViews((vs) => vs.filter((v) => v.id !== active.id))
      onApply(null)
    } catch (e) {
      fail(e)
    }
  }

  return (
    <div className="flex items-center gap-1.5">
      <select
        aria-label="Saved view"
        value={active ? String(active.id) : ''}
        onChange={(e) => onApply(views.find((v) => String(v.id) === e.target.value) ?? null)}
        className="h-7 max-w-48 rounded border border-input bg-input px-1.5 text-[12px] text-foreground"
      >
        <option value="">{views.length ? 'Views…' : 'No saved views'}</option>
        {views.map((v) => (
          <option key={v.id} value={String(v.id)}>
            {v.name}
            {!v.mine ? ` · ${v.owner_email?.split('@')[0] ?? 'shared'}` : v.shared ? ' · shared' : ''}
          </option>
        ))}
      </select>
      {active?.mine && (
        <>
          <Button size="xs" variant="ghost" onClick={() => void update()} title="Save the current filters into this view">
            Update
          </Button>
          <Button size="xs" variant="ghost" onClick={() => void remove()}>
            Delete
          </Button>
        </>
      )}
      <Button
        size="xs"
        variant="outline"
        onClick={() => {
          setName('')
          setShared(false)
          setSaving(true)
        }}
      >
        Save view…
      </Button>
      <Dialog open={saving} onOpenChange={(o: boolean) => setSaving(o)}>
        <DialogContent className="w-[calc(100%-2rem)] max-w-sm bg-card">
          <DialogTitle className="text-sm font-semibold text-foreground">Save view</DialogTitle>
          <form
            className="grid gap-3 text-[13px]"
            onSubmit={(e) => {
              e.preventDefault()
              void save()
            }}
          >
            <Input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="View name" aria-label="View name" />
            <label className="inline-flex items-center gap-2 text-[12px] text-foreground-secondary">
              <input type="checkbox" checked={shared} onChange={(e) => setShared(e.target.checked)} className="accent-primary" />
              Share with the workspace
            </label>
            <DialogFooter>
              <Button type="button" size="sm" variant="ghost" onClick={() => setSaving(false)}>
                Cancel
              </Button>
              <Button type="submit" size="sm" disabled={!name.trim() || busy}>
                Save
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  )
}
