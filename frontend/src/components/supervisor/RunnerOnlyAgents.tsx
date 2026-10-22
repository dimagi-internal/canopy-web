import { useState, type JSX } from 'react'
import { setRunnerOnlyAgents, type RunnerOut } from '@/api/harness'
import { SettingRow } from '@/components/supervisor/SettingRow'

// The agents a person dedicated this runner to (`Runner.only_agents`). Listed, the
// box runs only their turns and chats — no other agent's, no repo turn, no
// agentless chat, not even a pin. It never adds work: an agent still reaches the
// box through its own runner list. Owner or a runner administrator.
export function RunnerOnlyAgents({
  runner,
  onChange,
}: {
  runner: RunnerOut
  onChange: (fresh: RunnerOut) => void
}): JSX.Element {
  const current = Array.from(runner.only_agents ?? [])
  const [draft, setDraft] = useState(current.join(', '))
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const wanted = draft.split(/[\s,]+/).filter(Boolean)
  const dirty = wanted.slice().sort().join(',') !== current.slice().sort().join(',')

  const save = () => {
    if (saving || !dirty) return
    setSaving(true)
    setErr(null)
    setRunnerOnlyAgents(runner.id, wanted)
      .then((fresh) => {
        setDraft(Array.from(fresh.only_agents ?? []).join(', '))
        onChange(fresh)
      })
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not change it'))
      .finally(() => setSaving(false))
  }

  return (
    <SettingRow
      label="Only these agents"
      testId="runner-only-agents"
      help={
        current.length
          ? `Runs only ${current.join(', ')} — nothing else, not even a pin.`
          : 'Off — runs any agent routed to it.'
      }
      control={
        <button
          type="button"
          onClick={save}
          disabled={saving || !dirty}
          className="rounded-md border border-border px-2 py-1 text-[12px] disabled:opacity-50"
          data-testid="runner-only-agents-save"
        >
          Save
        </button>
      }
    >
      <input
        type="text"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && save()}
        placeholder="agent slugs, e.g. labsdata"
        aria-label="Only these agents"
        className="rounded-md border border-border bg-background px-2 py-1 text-[12px]"
        data-testid="runner-only-agents-input"
      />
      {err && <p className="text-[12px] text-destructive" data-testid="runner-only-agents-error">{err}</p>}
    </SettingRow>
  )
}
