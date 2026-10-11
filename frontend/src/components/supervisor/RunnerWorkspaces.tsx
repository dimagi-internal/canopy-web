import { useCallback, useEffect, useRef, useState, type JSX } from 'react'
import {
  enrollRunnerWorkspace,
  listRunnerWorkspaces,
  unenrollRunnerWorkspace,
  type RunnerEnrollment,
} from '@/api/harness'
import { useWorkspaceList } from '@/workspace/WorkspaceProvider'

// The server's own words ("this runner's owner is not an editor …"), not the
// JSON envelope `unwrap` wraps them in.
function reason(err: unknown): string {
  const text = err instanceof Error ? err.message : String(err)
  const match = /"detail":"((?:[^"\\]|\\.)*)"/.exec(text)
  return match ? match[1] : text
}

// Which workspaces this box SERVES. A runner takes a workspace's work — its
// agents' turns, its repo turns, its unbound chats — only when it is enrolled
// there AND its owner is an editor or above there. Pairing enrolls a box only in
// the workspace it paired into, and joining a workspace never enrolls the boxes
// you already have: a family workspace must not quietly get the Dimagi laptop.
//
// Owner or administrator (the panel is mounted only for them); an administrator
// sees and offers only workspaces they are a member of — the server filters, and
// the picker lists the viewer's own workspaces.
export function RunnerWorkspaces({ runnerId }: { runnerId: string }): JSX.Element {
  const [rows, setRows] = useState<RunnerEnrollment[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [pick, setPick] = useState('')
  const [busy, setBusy] = useState(false)
  const mine = useWorkspaceList()
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])

  const load = useCallback(() => {
    listRunnerWorkspaces(runnerId)
      .then((r) => { if (alive.current) setRows(r) })
      .catch((e) => { if (alive.current) { setRows([]); setErr(reason(e)) } })
  }, [runnerId])
  useEffect(load, [load])

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setErr(null)
    try {
      await fn()
      if (alive.current) { setPick(''); load() }
    } catch (e) {
      if (alive.current) setErr(reason(e))
    } finally {
      if (alive.current) setBusy(false)
    }
  }

  const enrolled = new Set((rows ?? []).map((r) => r.workspace))
  const offer = mine.filter((w) => !enrolled.has(w.slug))

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3" data-testid="runner-workspaces">
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Serves workspaces</span>
      {rows === null ? (
        <div className="h-6 w-40 animate-pulse rounded-md bg-muted" />
      ) : rows.length === 0 ? (
        <p className="text-[12px] text-muted-foreground" data-testid="runner-workspaces-empty">
          None — this box claims no work anywhere.
        </p>
      ) : (
        <ul className="flex flex-col gap-1">
          {rows.map((r) => (
            <li key={r.workspace} className="flex items-center justify-between gap-2 text-[13px]"
                data-testid={`runner-workspace-${r.workspace}`}>
              <span>
                {r.display_name || r.workspace}
                {!r.serving && (
                  <span className="ml-2 text-[11px] text-muted-foreground">
                    not serving — the owner is no longer an editor there
                  </span>
                )}
              </span>
              <button type="button" disabled={busy}
                      className="text-[12px] text-muted-foreground hover:text-destructive disabled:opacity-50"
                      onClick={() => act(() => unenrollRunnerWorkspace(runnerId, r.workspace))}>
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
      {offer.length > 0 && (
        <div className="flex items-center gap-2">
          <select value={pick} onChange={(e) => setPick(e.target.value)} disabled={busy}
                  className="h-8 rounded-md border border-border bg-background px-2 text-[13px]"
                  data-testid="runner-workspaces-pick">
            <option value="">Add a workspace…</option>
            {offer.map((w) => <option key={w.slug} value={w.slug}>{w.display_name || w.slug}</option>)}
          </select>
          <button type="button" disabled={busy || !pick}
                  className="h-8 rounded-md border border-border px-3 text-[13px] disabled:opacity-50"
                  data-testid="runner-workspaces-add"
                  onClick={() => act(() => enrollRunnerWorkspace(runnerId, pick))}>
            Enroll
          </button>
        </div>
      )}
      {err && <p className="text-[12px] text-destructive" data-testid="runner-workspaces-error">{err}</p>}
      <p className="text-[11px] text-foreground-subtle">
        A box works only in the workspaces listed here, and only while its owner is an editor or
        above in each. Joining a workspace does not add your boxes to it.
      </p>
    </div>
  )
}
