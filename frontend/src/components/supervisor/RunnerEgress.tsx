import { useState, type JSX } from 'react'
import { setRunnerEgress, type RunnerOut } from '@/api/harness'
import { SettingRow } from '@/components/supervisor/SettingRow'

// A cloud runner's egress lock (`Runner.egress_locked` / `egress_allow`). Locked,
// its runners reach the network only through the box's proxy: canopy-web, the model,
// GitHub, plus the hosts listed here. Root on the box applies it within a minute,
// on top of what the box itself is set to, never below. A sensitive-data box is always locked
// (`egress_forced`); adding a host to one asks for a passkey. Owner or a runner
// administrator.
export function RunnerEgress({
  runner,
  onChange,
}: {
  runner: RunnerOut
  onChange: (fresh: RunnerOut) => void
}): JSX.Element {
  const forced = Boolean(runner.egress_forced)
  const currentLocked = Boolean(runner.egress_locked) || forced
  const currentAllow = Array.from(runner.egress_allow ?? [])
  const [locked, setLocked] = useState(currentLocked)
  const [draft, setDraft] = useState(currentAllow.join(', '))
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const allow = draft.split(/[\s,]+/).filter(Boolean)
  const dirty =
    locked !== currentLocked || allow.slice().sort().join(',') !== currentAllow.slice().sort().join(',')

  const save = () => {
    if (saving || !dirty) return
    setSaving(true)
    setErr(null)
    setRunnerEgress(runner.id, locked, allow)
      .then((fresh) => {
        setLocked(Boolean(fresh.egress_locked) || Boolean(fresh.egress_forced))
        setDraft(Array.from(fresh.egress_allow ?? []).join(', '))
        onChange(fresh)
      })
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not change it'))
      .finally(() => setSaving(false))
  }

  const help = !currentLocked
    ? 'Open — its agents reach any host.'
    : `Locked — only canopy-web, the model, GitHub${currentAllow.length ? `, ${currentAllow.join(', ')}` : ''}.` +
      (forced ? ' Always locked on a sensitive-data box.' : '')

  return (
    <SettingRow
      label="Egress lock"
      testId="runner-egress"
      help={help}
      control={
        <button
          type="button"
          onClick={save}
          disabled={saving || !dirty}
          className="rounded-md border border-border px-2 py-1 text-[12px] disabled:opacity-50"
          data-testid="runner-egress-save"
        >
          Save
        </button>
      }
    >
      <label className="flex items-center gap-2 text-[12px]">
        <input
          type="checkbox"
          checked={locked}
          disabled={forced}
          onChange={(e) => setLocked(e.target.checked)}
          data-testid="runner-egress-locked"
        />
        Locked
      </label>
      <input
        type="text"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && save()}
        placeholder="extra hosts, e.g. slack.com, .googleapis.com"
        aria-label="Extra hosts"
        className="rounded-md border border-border bg-background px-2 py-1 text-[12px]"
        data-testid="runner-egress-allow"
      />
      {err && <p className="text-[12px] text-destructive" data-testid="runner-egress-error">{err}</p>}
    </SettingRow>
  )
}
