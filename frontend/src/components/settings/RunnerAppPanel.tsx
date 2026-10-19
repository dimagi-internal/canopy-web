import { useEffect, useState } from 'react'
import {
  getRunnerAppPreference,
  setRunnerAppPreference,
  type RunnerAppPage,
} from '@/api/runnerApp'

const PAGES: { value: RunnerAppPage; label: string }[] = [
  { value: 'supervisor', label: 'Supervisor' },
  { value: 'ide', label: 'IDE' },
]

/**
 * "Canopy Runner app opens on…" — the page the runner's desktop window loads
 * (it opens /runner-app/, which redirects to this choice; apps/harness/views.py).
 */
export function RunnerAppPanel() {
  const [opensOn, setOpensOn] = useState<RunnerAppPage | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    getRunnerAppPreference()
      .then((pref) => setOpensOn(pref.opens_on))
      .catch(() => setOpensOn(null))
  }, [])

  if (opensOn === null) return null

  const change = async (value: RunnerAppPage) => {
    setSaving(true)
    setError(null)
    try {
      setOpensOn((await setRunnerAppPreference(value)).opens_on)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'could not save')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-3" data-testid="settings-runner-app">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Runner app</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          The page the Canopy Runner desktop app opens when you click its menu-bar or Dock icon, on
          every box you use.
        </p>
      </div>
      <label className="flex flex-col gap-1 text-[12px] text-muted-foreground">
        Opens on
        <select
          value={opensOn}
          disabled={saving}
          onChange={(e) => void change(e.target.value as RunnerAppPage)}
          data-testid="runner-app-select"
          className="max-w-xs rounded-md border border-input bg-input px-2 py-1 text-[13px] text-foreground"
        >
          {PAGES.map((p) => (
            <option key={p.value} value={p.value}>
              {p.label}
            </option>
          ))}
        </select>
      </label>
      {error && <p className="text-[12px] text-destructive">{error}</p>}
    </div>
  )
}
