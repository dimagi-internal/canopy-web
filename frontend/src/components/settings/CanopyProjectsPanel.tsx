import { useEffect, useState } from 'react'
import {
  getCanopyProjects,
  getSessionSharing,
  setCanopyProjects,
  setSessionSharing,
  type CanopyProjects,
  type SessionShare,
} from '@/api/canopyProjects'

/**
 * "Use canopy in…" — every repo, or only the ones you tick
 * (apps/harness/models.py::ProjectOptIn). Everything else on your boxes is plain
 * Claude Code: canopy routes nothing to it and mirrors nothing from it. Lists the
 * repos your own runners report, plus any you type in.
 */
export function CanopyProjectsPanel() {
  const [saved, setSaved] = useState<CanopyProjects | null>(null)
  const [everything, setEverything] = useState(false)
  const [chosen, setChosen] = useState<Set<string>>(new Set())
  const [extra, setExtra] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = (p: CanopyProjects) => {
    setSaved(p)
    setEverything(p.everything)
    setChosen(new Set(p.projects))
  }

  useEffect(() => {
    getCanopyProjects()
      .then(load)
      .catch((e) => setError(e instanceof Error ? e.message : 'could not load'))
  }, [])

  if (saved === null) {
    return error ? (
      <div className="rounded-xl border border-border bg-card p-5 text-[12px] text-destructive">{error}</div>
    ) : null
  }

  const repos = [...new Set([...saved.available, ...chosen])].sort()
  const dirty =
    everything !== saved.everything ||
    chosen.size !== saved.projects.length ||
    saved.projects.some((p) => !chosen.has(p))

  const toggle = (repo: string) => {
    const next = new Set(chosen)
    if (next.has(repo)) next.delete(repo)
    else next.add(repo)
    setChosen(next)
  }

  const add = () => {
    const name = extra.trim()
    if (!name) return
    setChosen(new Set([...chosen, name]))
    setExtra('')
  }

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      load(await setCanopyProjects(everything, [...chosen]))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'could not save')
    } finally {
      setSaving(false)
    }
  }

  const nothing = !everything && chosen.size === 0

  return (
    <div id="canopy-projects" className="rounded-xl border border-border bg-card p-5 space-y-3" data-testid="settings-canopy-projects">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Opted-in repos</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          The repos you use canopy in. Every other repo on your machines stays plain Claude Code:
          canopy sends no work there and mirrors no sessions, transcripts or usage from it. An
          agent&apos;s own repo always counts.
        </p>
      </div>
      <fieldset className="space-y-1 text-[13px] text-foreground" disabled={saving}>
        <label className="flex items-center gap-2">
          <input
            type="radio"
            name="canopy-projects-mode"
            checked={everything}
            onChange={() => setEverything(true)}
            data-testid="canopy-projects-everything"
          />
          Every repo
        </label>
        <label className="flex items-center gap-2">
          <input
            type="radio"
            name="canopy-projects-mode"
            checked={!everything}
            onChange={() => setEverything(false)}
            data-testid="canopy-projects-chosen"
          />
          Only the repos I choose
        </label>
      </fieldset>
      {!everything && (
        <div className="space-y-2">
          {repos.length === 0 ? (
            <p className="text-[12px] text-muted-foreground">
              None of your runners has reported a repo yet. Type a repo name to add it.
            </p>
          ) : (
            <ul className="divide-y divide-border rounded-md border border-border">
              {repos.map((repo) => (
                <li key={repo}>
                  <label className="flex items-center gap-2 px-2 py-1 text-[13px] text-foreground">
                    <input
                      type="checkbox"
                      checked={chosen.has(repo)}
                      disabled={saving}
                      onChange={() => toggle(repo)}
                      data-testid={`canopy-project-${repo}`}
                    />
                    <span className="font-mono">{repo}</span>
                    {!saved.available.includes(repo) && (
                      <span className="text-[11px] text-muted-foreground">not on your runners yet</span>
                    )}
                  </label>
                </li>
              ))}
            </ul>
          )}
          <div className="flex gap-2">
            <input
              value={extra}
              onChange={(e) => setExtra(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') add()
              }}
              placeholder="Add a repo by name"
              data-testid="canopy-projects-add"
              className="max-w-xs flex-1 rounded-md border border-input bg-input px-2 py-1 text-[13px] text-foreground"
            />
            <button
              type="button"
              onClick={add}
              className="rounded-md border border-border px-2 py-1 text-[12px] text-foreground hover:bg-muted"
            >
              Add
            </button>
          </div>
        </div>
      )}
      <div className="flex items-center gap-3">
        <button
          type="button"
          disabled={!dirty || saving}
          onClick={() => void save()}
          data-testid="canopy-projects-save"
          className="rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save'}
        </button>
        {nothing && !dirty && (
          <p className="text-[12px] text-warning">
            Canopy is off in every repo: your runners take no repo work and mirror no sessions.
          </p>
        )}
      </div>
      {error && <p className="text-[12px] text-destructive">{error}</p>}
      <SessionSharingSection />
    </div>
  )
}

/**
 * "Share my sessions with the project team" — per project, off by default. A
 * session on your laptop is yours alone; turning a project on shows your sessions
 * in its repo to that project's team, who can read and work in them
 * (apps/canopy_sessions/sharing.py, ProjectSessionShare).
 */
function SessionSharingSection() {
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
    <div className="space-y-2 border-t border-border pt-3" data-testid="settings-session-sharing">
      <div>
        <h3 className="text-[13px] font-semibold text-foreground">Share my sessions with a project team</h3>
        <p className="mt-1 text-[12px] text-muted-foreground">
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
