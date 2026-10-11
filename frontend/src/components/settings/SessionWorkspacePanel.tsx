import { useEffect, useState } from 'react'
import {
  getSessionWorkspacePreference,
  setSessionWorkspacePreference,
  type SessionWorkspacePreference,
} from '@/api/sessionWorkspace'

/**
 * "File my emdash sessions in…" — the default workspace for NEW sessions a
 * runner you own discovers (apps/canopy_sessions/filing.py). An agent's project
 * still follows the agent; existing sessions stay put (move one from its ⋯ menu).
 * Hidden for someone who can only file in one workspace: there is no choice.
 */
export function SessionWorkspacePanel() {
  const [pref, setPref] = useState<SessionWorkspacePreference | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getSessionWorkspacePreference()
      .then(setPref)
      .catch(() => setPref(null))
  }, [])

  if (pref === null || pref.choices.length < 2) return null

  const change = async (value: string) => {
    setSaving(true)
    setError(null)
    try {
      setPref(await setSessionWorkspacePreference(value || null))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'could not save')
    } finally {
      setSaving(false)
    }
  }

  const lapsed = pref.workspace !== null && pref.effective === null

  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-3" data-testid="settings-session-workspace">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Session workspace</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Where new sessions from your own runners are filed. A session in an agent&apos;s project
          always goes to that agent&apos;s workspace. Existing sessions stay where they are — move one
          from its ⋯ menu.
        </p>
      </div>
      <label className="flex flex-col gap-1 text-[12px] text-muted-foreground">
        File my sessions in
        <select
          value={pref.workspace ?? ''}
          disabled={saving}
          onChange={(e) => void change(e.target.value)}
          data-testid="session-workspace-select"
          className="max-w-xs rounded-md border border-input bg-input px-2 py-1 text-[13px] text-foreground"
        >
          <option value="">Each runner&apos;s own workspace</option>
          {pref.choices.map((c) => (
            <option key={c.slug} value={c.slug}>
              {c.display_name}
            </option>
          ))}
        </select>
      </label>
      {lapsed && (
        <p className="text-[12px] text-warning">
          You can no longer edit in {pref.workspace}, so new sessions go to each runner&apos;s own
          workspace until you pick another.
        </p>
      )}
      {error && <p className="text-[12px] text-destructive">{error}</p>}
    </div>
  )
}
