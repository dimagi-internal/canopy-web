import { useEffect, useState } from 'react'
import { getSessionSharing, setSessionSharing, type SessionShare } from '@/api/canopyProjects'

/**
 * The project teams you still share your laptop sessions with — shown only to
 * someone who has such a share, so they can end it. Starting a new one is
 * retired (apps/canopy_sessions/sharing.py): the "team" was the project's
 * participants, a list canopy grows by itself, so it never matched what a
 * person thought they agreed to. To share one session, add people on its page.
 */
export function SessionSharingPanel() {
  const [rows, setRows] = useState<SessionShare[] | null>(null)
  const [busy, setBusy] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getSessionSharing()
      .then(setRows)
      .catch(() => setRows([]))
  }, [])

  const stop = async (row: SessionShare) => {
    setBusy(row.project_id)
    setError(null)
    try {
      await setSessionSharing(row.project_id, false)
      setRows((prev) => (prev ?? []).filter((r) => r.project_id !== row.project_id))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'could not save')
    } finally {
      setBusy(null)
    }
  }

  if (!rows || rows.length === 0) return null
  return (
    <div id="session-sharing" className="rounded-xl border border-border bg-card p-5 space-y-3" data-testid="settings-session-sharing">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Laptop sessions you still share with a project team</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Every session on your own machines in these repos can be read, worked in and pulled from your
          machine by the project&apos;s owner and anyone canopy counts as taking part in it — a list that
          grows as people reply on its tasks or join its check-ins. This setting is retired: you can stop
          sharing, but not start again. To share one session, add people to it from its page.
        </p>
      </div>
      <ul className="divide-y divide-border rounded-md border border-border">
        {rows.map((row) => (
          <li key={row.project_id} className="flex items-center gap-2 px-2 py-1.5 text-[13px] text-foreground">
            <span>{row.name}</span>
            <span className="font-mono text-[12px] text-muted-foreground">{row.repo}</span>
            <span className="text-[11px] text-muted-foreground">
              {row.workspace} · {row.team_size} {row.team_size === 1 ? 'person' : 'people'} today
            </span>
            <button
              type="button"
              className="ml-auto rounded-md border border-border px-2 py-0.5 text-[12px] hover:bg-muted disabled:opacity-50"
              disabled={busy !== null}
              onClick={() => void stop(row)}
              data-testid={`session-sharing-stop-${row.project_id}`}
            >
              Stop sharing
            </button>
          </li>
        ))}
      </ul>
      {error && <p className="text-[12px] text-destructive">{error}</p>}
    </div>
  )
}
