import { useCallback, useEffect, useState } from 'react'

import {
  NeedsConfirm,
  createMyCollection,
  deleteMyCollection,
  listMyCollections,
  setMyGrantCollection,
  updateMyCollection,
} from '../api/people'
import type { HcpGrant, MemoryCollections } from '../api/people'

const BUTTON = 'rounded-md border border-border px-2 py-1 text-xs text-foreground hover:bg-muted disabled:opacity-50'

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

/** "More settings" on Your memory: collections — named sets of contexts (an
 *  organisation you belong to, your personal memory) — and which one each agent
 *  you keep allowing uses. By default an agent reaches its own organisation's
 *  memory; nothing here is ever asked in a conversation. */
export function MemorySettings({ grants, onChanged }: { grants: HcpGrant[]; onChanged: () => void }) {
  const [data, setData] = useState<MemoryCollections | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [name, setName] = useState('')
  const [picked, setPicked] = useState<string[]>([])

  const load = useCallback(() => {
    listMyCollections()
      .then(setData)
      .catch((e: Error) => setError(e.message))
  }, [])
  useEffect(load, [load])

  const act = async (run: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await run()
      load()
      onChanged()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const kept = grants.filter((g) => g.status === 'active' && g.canopy.agent && g.grantType === 'persistent')
  const contexts = data?.contexts ?? []
  const labelOf = (source: string) => contexts.find((c) => c.source === source)?.label ?? source

  return (
    <details className="rounded-lg border border-border px-4 py-3">
      <summary className="cursor-pointer text-sm font-semibold text-foreground">More settings</summary>
      <div className="mt-3 space-y-4 text-sm">
        {error && <p className="text-destructive">{error}</p>}

        <section aria-label="What each agent can use">
          <h3 className="font-semibold text-foreground">What each agent can use</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">
            An agent you allow uses its own organisation&apos;s memory. Point it at a collection to let it
            use something else, such as your personal memory.
          </p>
          {kept.length === 0 ? (
            <p className="mt-1 text-xs text-muted-foreground">No agent is kept allowed yet.</p>
          ) : (
            <ul className="mt-1 divide-y divide-border rounded-md border border-border">
              {kept.map((g) => (
                <li key={g.grantId} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
                  <span className="text-foreground">{g.client.name}</span>
                  <label className="flex items-center gap-1 text-xs text-muted-foreground">
                    uses
                    <select
                      aria-label={`What ${g.client.name} uses`}
                      disabled={busy}
                      value={g.canopy.collection?.id ?? ''}
                      onChange={(e) => {
                        const id = e.target.value === '' ? null : Number(e.target.value)
                        void act(() => withConfirm((c) => setMyGrantCollection(g.grantId, id, c)))
                      }}
                      className="rounded-md border border-border bg-background px-1 py-0.5 text-foreground"
                    >
                      <option value="">its organisation (default)</option>
                      {(data?.collections ?? []).map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.name}
                        </option>
                      ))}
                    </select>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section aria-label="Collections">
          <h3 className="font-semibold text-foreground">Collections</h3>
          <ul className="mt-1 space-y-2">
            {(data?.collections ?? []).map((c) => (
              <li key={c.id} className="rounded-md border border-border px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium text-foreground">{c.name}</span>
                  <button
                    type="button"
                    className={BUTTON}
                    disabled={busy}
                    onClick={() => {
                      if (window.confirm(`Remove ${c.name}?`)) void act(() => deleteMyCollection(c.id))
                    }}
                  >
                    Remove
                  </button>
                </div>
                <div className="mt-1 flex flex-wrap gap-3 text-xs">
                  {contexts.map((ctx) => {
                    const on = c.sources.includes(ctx.source)
                    return (
                      <label key={ctx.source} className="flex items-center gap-1 text-muted-foreground">
                        <input
                          type="checkbox"
                          checked={on}
                          disabled={busy}
                          onChange={() => {
                            const next = on ? c.sources.filter((s) => s !== ctx.source) : [...c.sources, ctx.source]
                            void act(() => withConfirm((conf) => updateMyCollection(c.id, { sources: next }, conf)))
                          }}
                        />
                        {ctx.label}
                      </label>
                    )
                  })}
                </div>
                {(c.used_by ?? []).length > 0 && (
                  <p className="mt-1 text-xs text-muted-foreground">
                    Used by {Array.from(new Set((c.used_by ?? []).map((u) => u.name))).join(', ')}
                  </p>
                )}
              </li>
            ))}
          </ul>
          <form
            className="mt-2 flex flex-wrap items-center gap-2"
            aria-label="New collection"
            onSubmit={(e) => {
              e.preventDefault()
              if (!name.trim() || picked.length === 0) return
              void act(async () => {
                await createMyCollection(name.trim(), picked)
                setName('')
                setPicked([])
              })
            }}
          >
            <input
              aria-label="Collection name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="New collection"
              maxLength={80}
              className="min-w-0 rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground"
            />
            {contexts.map((ctx) => (
              <label key={ctx.source} className="flex items-center gap-1 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={picked.includes(ctx.source)}
                  onChange={() =>
                    setPicked((p) => (p.includes(ctx.source) ? p.filter((s) => s !== ctx.source) : [...p, ctx.source]))
                  }
                />
                {labelOf(ctx.source)}
              </label>
            ))}
            <button type="submit" className={BUTTON} disabled={busy || !name.trim() || picked.length === 0}>
              Add
            </button>
          </form>
        </section>
      </div>
    </details>
  )
}
