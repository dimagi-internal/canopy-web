import { useEffect, useState } from 'react'
import { getSessionSharing, setSessionSharing, type SessionShare } from '@/api/canopyProjects'

/**
 * "Share my sessions with the project team" — per project, off by default. A
 * session on your laptop is yours alone; turning a project on shows your sessions
 * in its repo to that project's team, who can read and work in them
 * (apps/canopy_sessions/sharing.py, ProjectSessionShare).
 */
export function SessionSharingPanel() {
  const [rows, setRows] = useState<SessionShare[] | null>(null)
  const [busy, setBusy] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getSessionSharing()
      .then(setRows)
      .catch((e) => setError(e instanceof Error ? e.message : 'could not load'))
  }, [])

  const toggle = async (row: SessionShare) => {
    setBusy(row.project_id)
    setError(null)
    try {
      const next = await setSessionSharing(row.project_id, !row.shared)
      setRows((prev) => (prev ?? []).map((r) => (r.project_id === next.project_id ? next : r)))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'could not save')
    } finally {
      setBusy(null)
    }
  }

  if (rows === null && !error) return null
  return (
    <div id="session-sharing" className="rounded-xl border border-border bg-card p-5 space-y-3" data-testid="settings-session-sharing">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Share my sessions with a project team</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Sessions on your machines are yours alone. Turn a project on and your sessions in its repo
          are visible to that project&apos;s team, who can read and work in them. Agents never see
          them this way. Turn it off at any time.
        </p>
      </div>
      {rows && rows.length === 0 ? (
        <p className="text-[12px] text-muted-foreground">No project in your workspaces works in one of your repos.</p>
      ) : (
        <ul className="divide-y divide-border rounded-md border border-border">
          {(rows ?? []).map((row) => (
            <li key={row.project_id}>
              <label className="flex items-center gap-2 px-2 py-1 text-[13px] text-foreground">
                <input
                  type="checkbox"
                  checked={row.shared}
                  disabled={busy !== null}
                  onChange={() => void toggle(row)}
                  data-testid={`session-sharing-${row.project_id}`}
                />
                <span>{row.name}</span>
                <span className="font-mono text-[12px] text-muted-foreground">{row.repo}</span>
                <span className="text-[11px] text-muted-foreground">
                  {row.workspace} · team of {row.team_size}
                </span>
              </label>
            </li>
          ))}
        </ul>
      )}
      {error && <p className="text-[12px] text-destructive">{error}</p>}
    </div>
  )
}
