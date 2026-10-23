import { useEffect, useState } from 'react'
import { getCanopyProjects, setCanopyProjects, type CanopyProjects } from '@/api/canopyProjects'

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
    <div className="rounded-xl border border-border bg-card p-5 space-y-3" data-testid="settings-canopy-projects">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Canopy projects</h2>
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
    </div>
  )
}
