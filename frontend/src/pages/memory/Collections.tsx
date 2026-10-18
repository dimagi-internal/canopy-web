import { useState } from 'react'
import { Layers, Plus, Trash2 } from 'lucide-react'

import {
  NeedsConfirm,
  createMyCollection,
  deleteMyCollection,
  setMyGrantCollection,
  updateMyCollection,
} from '../../api/people'
import type { HcpGrant, MemoryCollections } from '../../api/people'

/** Run a change; if canopy says it lets agents reach more, ask, then resend with
 *  the grants it named (HCP 4.1.5.1 — widening is only ever the person's act). */
async function withConfirm(run: (confirm?: string[]) => Promise<unknown>): Promise<boolean> {
  try {
    await run()
    return true
  } catch (e) {
    if (!(e instanceof NeedsConfirm)) throw e
    if (!window.confirm(`${e.message} Continue?`)) return false
    await run(e.grants)
    return true
  }
}

/** What an agent you keep allowing reads: its own organisation (the default), or
 *  one of your collections. Sits on the agent's card, where the question is asked. */
export function AgentReadsPicker({
  grant,
  data,
  disabled,
  act,
}: {
  grant: HcpGrant
  data: MemoryCollections | null
  disabled: boolean
  act: (run: () => Promise<unknown>) => void
}) {
  return (
    <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
      <Layers className="h-3.5 w-3.5" />
      Reads
      <select
        aria-label={`What ${grant.client.name} reads`}
        disabled={disabled}
        value={grant.canopy.collection?.id ?? ''}
        onChange={(e) => {
          const id = e.target.value === '' ? null : Number(e.target.value)
          act(() => withConfirm((c) => setMyGrantCollection(grant.grantId, id, c)))
        }}
        className="rounded-md border border-border bg-background px-1.5 py-0.5 text-xs text-foreground"
      >
        <option value="">its organisation (default)</option>
        {(data?.collections ?? []).map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </select>
    </label>
  )
}

/** Your collections: named bundles of memory (an organisation's, your personal)
 *  you can hand an agent instead of its default. Each shows what it holds and
 *  who uses it; ticking a context in or out is the whole edit. */
export function CollectionsPanel({
  data,
  busy,
  act,
}: {
  data: MemoryCollections | null
  busy: boolean
  act: (run: () => Promise<unknown>) => void
}) {
  const [name, setName] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const [adding, setAdding] = useState(false)
  const contexts = data?.contexts ?? []
  const collections = data?.collections ?? []

  const chip = (on: boolean) =>
    `inline-flex cursor-pointer items-center gap-1 rounded-full border px-2 py-0.5 text-xs transition-colors ${
      on ? 'border-primary/50 bg-primary/10 text-primary' : 'border-border text-muted-foreground hover:text-foreground'
    }`

  return (
    <div className="mt-6">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="text-sm font-semibold text-foreground">Collections</h3>
        {!adding && (
          <button className="inline-flex items-center gap-1 text-xs text-primary hover:underline" onClick={() => setAdding(true)}>
            <Plus className="h-3.5 w-3.5" /> New collection
          </button>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        A collection bundles memory you can hand an agent instead of its default — say, Dimagi plus your personal memory.
      </p>
      {collections.length === 0 && !adding ? (
        <p className="mt-2 rounded-xl border border-dashed border-border px-4 py-4 text-center text-xs text-muted-foreground">
          No collections yet. Every agent reads its own organisation's memory.
        </p>
      ) : (
        <ul className="mt-2 grid gap-3 sm:grid-cols-2">
          {collections.map((c) => (
            <li key={c.id} className="rounded-xl border border-border bg-card p-3">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium text-foreground">{c.name}</span>
                <button
                  type="button"
                  aria-label={`Remove ${c.name}`}
                  title="Remove"
                  className="rounded p-1 text-muted-foreground hover:bg-destructive/10 hover:text-destructive disabled:opacity-50"
                  disabled={busy}
                  onClick={() => {
                    if (window.confirm(`Remove ${c.name}?`)) act(() => withConfirm(() => deleteMyCollection(c.id)))
                  }}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
              <div className="mt-2 flex flex-wrap gap-1.5">
                {contexts.map((ctx) => {
                  const on = c.sources.includes(ctx.source)
                  return (
                    <label key={ctx.source} className={chip(on)}>
                      <input
                        type="checkbox"
                        className="sr-only"
                        checked={on}
                        disabled={busy}
                        onChange={() => {
                          const next = on ? c.sources.filter((s) => s !== ctx.source) : [...c.sources, ctx.source]
                          act(() => withConfirm((conf) => updateMyCollection(c.id, { sources: next }, conf)))
                        }}
                      />
                      {ctx.label}
                    </label>
                  )
                })}
              </div>
              <p className="mt-2 text-xs text-muted-foreground">
                {(c.used_by ?? []).length > 0
                  ? `Used by ${Array.from(new Set((c.used_by ?? []).map((u) => u.name))).join(', ')}`
                  : 'No agent uses it yet — pick it on an agent above.'}
              </p>
            </li>
          ))}
          {adding && (
            <li className="rounded-xl border border-primary/40 bg-card p-3">
              <form
                aria-label="New collection"
                onSubmit={(e) => {
                  e.preventDefault()
                  if (!name.trim() || picked.length === 0) return
                  act(async () => {
                    await createMyCollection(name.trim(), picked)
                    setName('')
                    setPicked([])
                    setAdding(false)
                  })
                }}
              >
                <input
                  autoFocus
                  aria-label="Collection name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  onKeyDown={(e) => e.key === 'Escape' && setAdding(false)}
                  placeholder="Name, e.g. Work + personal"
                  maxLength={80}
                  className="w-full rounded-md border border-border bg-background px-2 py-1 text-sm text-foreground"
                />
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {contexts.map((ctx) => {
                    const on = picked.includes(ctx.source)
                    return (
                      <label key={ctx.source} className={chip(on)}>
                        <input
                          type="checkbox"
                          className="sr-only"
                          checked={on}
                          onChange={() =>
                            setPicked((p) => (on ? p.filter((s) => s !== ctx.source) : [...p, ctx.source]))
                          }
                        />
                        {ctx.label}
                      </label>
                    )
                  })}
                </div>
                <div className="mt-3 flex justify-end gap-2">
                  <button type="button" className="text-xs text-muted-foreground hover:text-foreground" onClick={() => setAdding(false)}>
                    Cancel
                  </button>
                  <button
                    type="submit"
                    disabled={busy || !name.trim() || picked.length === 0}
                    className="rounded-md bg-primary px-2.5 py-1 text-xs font-medium text-primary-foreground disabled:opacity-50"
                  >
                    Create
                  </button>
                </div>
              </form>
            </li>
          )}
        </ul>
      )}
    </div>
  )
}
