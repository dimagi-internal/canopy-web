import { useEffect, useState } from 'react'

import { addMyViewer, listMyViewers, removeMyViewer } from '../../api/people'
import type { MemoryViewers } from '../../api/people'

/** "Who can see this page": agents you let read everything on it, read-only. */
export function PageViewers() {
  const [data, setData] = useState<MemoryViewers | null>(null)
  const [pick, setPick] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    listMyViewers()
      .then(setData)
      .catch((e: Error) => setError(e.message))
  }, [])

  const act = async (work: () => Promise<MemoryViewers>) => {
    setBusy(true)
    setError(null)
    try {
      setData(await work())
      setPick('')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (!data) return error ? <p className="text-sm text-destructive">{error}</p> : null
  return (
    <div className="rounded-xl border border-border p-4">
      <h3 className="text-sm font-semibold text-foreground">Who can see this page</h3>
      <p className="text-xs text-muted-foreground">
        An agent you add here can read everything on this page — what is held, who can read it, what happened and how agents
        searched it — through its own login. It cannot change anything. Each time it looks shows in What happened.
      </p>
      {data.viewers.length > 0 && (
        <ul className="mt-3 space-y-2">
          {data.viewers.map((v) => (
            <li key={v.agent} className="flex items-center justify-between gap-3 text-sm">
              <span>
                <span className="font-medium text-foreground">{v.name}</span>{' '}
                <span className="text-xs text-muted-foreground">
                  since {new Date(v.granted_at).toLocaleDateString()}
                  {v.last_viewed_at ? ` · last looked ${new Date(v.last_viewed_at).toLocaleString()}` : ' · has not looked yet'}
                </span>
              </span>
              <button
                className="text-xs text-destructive hover:underline disabled:opacity-50"
                disabled={busy}
                onClick={() => void act(() => removeMyViewer(v.agent))}
              >
                Stop
              </button>
            </li>
          ))}
        </ul>
      )}
      {data.agents.length > 0 && (
        <div className="mt-3 flex items-center gap-2">
          <select
            aria-label="Agent"
            value={pick}
            onChange={(e) => setPick(e.target.value)}
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
          >
            <option value="">Choose an agent…</option>
            {data.agents.map((a) => (
              <option key={a.slug} value={a.slug}>
                {a.name}
              </option>
            ))}
          </select>
          <button
            className="rounded-md bg-primary px-3 py-1 text-sm text-primary-foreground disabled:opacity-50"
            disabled={!pick || busy}
            onClick={() => void act(() => addMyViewer(pick))}
          >
            Let it see this page
          </button>
        </div>
      )}
      {error && <p role="alert" className="mt-2 text-xs text-destructive">{error}</p>}
    </div>
  )
}
