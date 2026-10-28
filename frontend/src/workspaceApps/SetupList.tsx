import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getSetup, updateSetup, type AppSetup } from '@/api/workspaceApps'
import { useScopedPath } from '@/lib/scopedLinks'

/**
 * The app's first-run steps (manifest `setup`), shown to whoever maintains it on
 * top of the app until every step is done or they put it away. Most steps tick
 * themselves — the app reads its own state (a role given, a job opened, the
 * public pages on) — so the list is never a second thing to keep up to date.
 */
export function SetupList({ workspace, appKey }: { workspace: string; appKey: string }) {
  const scoped = useScopedPath()
  const [setup, setSetup] = useState<AppSetup | null>(null)
  const [open, setOpen] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    getSetup(workspace, appKey).then((s) => live && setSetup(s), () => live && setSetup(null))
    return () => { live = false }
  }, [workspace, appKey])

  async function change(body: { step?: string; done?: boolean; hidden?: boolean }) {
    setError(null)
    try { setSetup(await updateSetup(workspace, appKey, body)) } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }

  if (!setup || setup.steps.length === 0) return null
  const done = setup.steps.filter((s) => s.done).length
  if (done === setup.steps.length) return null
  if (setup.hidden) {
    return (
      <div className="mb-3 text-right text-xs">
        <button type="button" className="text-muted-foreground hover:underline" onClick={() => change({ hidden: false })}>
          Show the setup list ({done} of {setup.steps.length} done)
        </button>
      </div>
    )
  }
  const href = (go: string | null | undefined) => go && scoped(`/apps/${appKey}/${go}`)

  return (
    <section aria-label="Set up this app" className="mb-4 rounded-lg border border-primary/30 bg-primary/5 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <button type="button" className="text-left" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          <span className="font-semibold">Set up this app</span>
          <span className="ml-2 text-sm text-muted-foreground">{done} of {setup.steps.length} done</span>
        </button>
        <button type="button" className="text-xs text-muted-foreground hover:underline" onClick={() => change({ hidden: true })}>
          Put this away
        </button>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted">
        <div className="h-full bg-primary" style={{ width: `${(100 * done) / setup.steps.length}%` }} />
      </div>
      {error && <p className="mt-2 text-sm text-destructive">{error}</p>}
      {open && (
        <ol className="mt-3 space-y-2">
          {setup.steps.map((s) => (
            <li key={s.id} className="flex items-start gap-3 text-sm">
              {s.manual ? (
                <input type="checkbox" className="mt-1" checked={s.done} aria-label={`Done: ${s.title}`}
                  onChange={(e) => change({ step: s.id, done: e.target.checked })} />
              ) : (
                <span aria-label={s.done ? 'done' : 'not done yet'}
                  className={`mt-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border text-[10px] ${s.done ? 'border-success bg-success text-white' : 'border-border'}`}>
                  {s.done ? '✓' : ''}
                </span>
              )}
              <div className="min-w-0">
                <div className={s.done ? 'text-muted-foreground line-through' : 'font-medium'}>
                  {s.title}
                  {!s.done && href(s.go) && <Link to={href(s.go) as string} className="ml-2 text-xs font-normal text-primary hover:underline">Go →</Link>}
                </div>
                {!s.done && s.body && <p className="text-xs text-muted-foreground">{s.body}</p>}
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
