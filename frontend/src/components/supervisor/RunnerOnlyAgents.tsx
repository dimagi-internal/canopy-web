import { useEffect, useRef, useState, type JSX } from 'react'
import { getRunnerOnlyAgents, setRunnerOnlyAgents, type RunnerOnlyAgents as Choices, type RunnerOut } from '@/api/harness'
import { SettingRow, Switch } from '@/components/supervisor/SettingRow'

// Allowlist agents (`Runner.only_agents`): on, the box runs only the ticked agents'
// turns and chats — no other agent's, no repo turn, no agentless chat, not even a
// pin. It never adds work: an agent still reaches the box through its own runner
// list. The choices are the server's: agents in the workspaces the box serves that
// you can see. Owner or a runner administrator.
export function RunnerOnlyAgents({
  runner,
  onChange,
}: {
  runner: RunnerOut
  onChange: (fresh: RunnerOut) => void
}): JSX.Element {
  const current = Array.from(runner.only_agents ?? [])
  // On while the list is saved non-empty, or while you are picking the first agent.
  const [picking, setPicking] = useState(false)
  const on = current.length > 0 || picking
  const [choices, setChoices] = useState<Choices['choices'] | null>(null)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])

  useEffect(() => {
    if (!on || choices !== null) return
    getRunnerOnlyAgents(runner.id)
      .then((c) => { if (alive.current) setChoices(c.choices) })
      .catch((e: unknown) => { if (alive.current) setErr(e instanceof Error ? e.message : 'could not load the agents') })
  }, [on, choices, runner.id])

  const save = (agents: string[]) => {
    setSaving(true)
    setErr(null)
    setRunnerOnlyAgents(runner.id, agents)
      .then((fresh) => {
        if (!alive.current) return
        if (agents.length > 0) setPicking(false)
        onChange(fresh)
      })
      .catch((e: unknown) => { if (alive.current) setErr(e instanceof Error ? e.message : 'could not change it') })
      .finally(() => { if (alive.current) setSaving(false) })
  }

  const toggle = () => {
    if (saving) return
    if (!on) {
      setPicking(true)
      return
    }
    setPicking(false)
    if (current.length > 0) save([])
  }

  const tick = (slug: string) => {
    if (saving) return
    const next = current.includes(slug) ? current.filter((s) => s !== slug) : [...current, slug]
    if (next.length === 0) setPicking(true) // unticking the last keeps the list open
    save(next)
  }

  return (
    <SettingRow
      label="Allowlist agents"
      testId="runner-only-agents"
      help={
        current.length
          ? `Runs only ${current.join(', ')} — no other agent, no repo work, not even a pin.`
          : on
            ? 'Tick the agents this box may run. Until you tick one it runs any agent routed to it.'
            : 'Off — runs any agent routed to it.'
      }
      control={<Switch checked={on} onChange={toggle} disabled={saving} label="Allowlist agents" testId="runner-only-agents-toggle" />}
    >
      {on && choices === null && !err && <p className="text-[12px] text-muted-foreground">Loading agents…</p>}
      {on && choices !== null && (
        <ul className="flex flex-col gap-1 pt-1" data-testid="runner-only-agents-list">
          {choices.length === 0 && (
            <li className="text-[12px] text-muted-foreground">No agents in the workspaces this box serves.</li>
          )}
          {choices.map((a) => (
            <li key={a.slug}>
              <label className="flex items-center gap-2 text-[12px]">
                <input
                  type="checkbox"
                  checked={current.includes(a.slug)}
                  onChange={() => tick(a.slug)}
                  disabled={saving}
                  data-testid={`runner-only-agents-${a.slug}`}
                />
                <span className="text-foreground">{a.name}</span>
                <span className="text-muted-foreground">{a.slug} · {a.workspace}</span>
              </label>
            </li>
          ))}
        </ul>
      )}
      {err && <p className="text-[12px] text-destructive" data-testid="runner-only-agents-error">{err}</p>}
    </SettingRow>
  )
}
