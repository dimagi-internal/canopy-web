import { useEffect, useState, type JSX } from 'react'
import {
  deleteTurnModeRule,
  getTurnModeRules,
  setTurnModeRule,
  type RoutableSource,
  type TurnMode,
  type TurnModeRuleOut,
} from '@/api/agents'
import { ACTORLESS_SOURCES, ROUTABLE_SOURCES, SOURCE_LABEL } from '@/components/agents/RunnerSourceRules'
import { TurnModeToggle } from '@/components/agents/TurnModeToggle'

// ONE TABLE for "may this agent act without asking first?" — its turn mode, per
// channel, per person, or both, above the agent's own switch ("Everything else").
//
// Separate from routing on purpose: which runner takes the work is the Routing
// table and needs rights over the box; this needs rights over the agent only, and
// names no runner (apps/harness/turn_mode.py, apps/agents/api_turn_mode.py).

const MODE_LABEL: Record<TurnMode, string> = { manual: 'Manual', auto: 'Auto' }

// The order the server reads them in (`turn_mode.resolve`): person on a channel,
// person on any channel, channel for anyone. Drawn in that order, the table reads
// top to bottom as the precedence.
function rank(r: { source: string; actor: string }): number {
  if (r.actor && r.source) return 0
  if (r.actor) return 1
  return 2
}

export function sortTurnModeRules(rules: readonly TurnModeRuleOut[]): TurnModeRuleOut[] {
  return [...rules].sort(
    (a, b) => rank(a) - rank(b) || a.actor.localeCompare(b.actor) || a.source.localeCompare(b.source),
  )
}

function channel(source: string): string {
  return source ? (SOURCE_LABEL[source] ?? source) : 'any channel'
}

export function AgentTurnModeRules({
  agentSlug,
  initialTurnMode,
  workspace,
  onSaved,
}: {
  agentSlug: string
  initialTurnMode: TurnMode
  /** The agent's workspace, when the page is under another. */
  workspace?: string
  onSaved?: () => void
}): JSX.Element {
  const [rules, setRules] = useState<TurnModeRuleOut[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)

  useEffect(() => {
    let cancelled = false
    setRules(null)
    getTurnModeRules(agentSlug, workspace)
      .then((r) => !cancelled && setRules(r))
      .catch((e: unknown) => {
        if (cancelled) return
        setError(e instanceof Error ? e.message : 'Failed to load')
        setRules([])
      })
    return () => {
      cancelled = true
    }
  }, [agentSlug, workspace])

  const run = async (fn: () => Promise<TurnModeRuleOut[]>) => {
    setError(null)
    try {
      setRules(await fn())
      onSaved?.()
      return true
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to save')
      return false
    }
  }
  const save = (source: string, actor: string, turn_mode: TurnMode) =>
    run(() => setTurnModeRule(agentSlug, { source: source as RoutableSource | '', actor, turn_mode }, workspace))
  const remove = (r: TurnModeRuleOut) =>
    run(async () => {
      await deleteTurnModeRule(agentSlug, r, workspace)
      return getTurnModeRules(agentSlug, workspace)
    })

  if (rules === null) {
    return <div className="h-16 w-full animate-pulse rounded-md bg-muted" data-testid="turn-mode-rules-loading" />
  }
  const sorted = sortTurnModeRules(rules)
  const namedAuto = rules.some((r) => r.actor && r.turn_mode === 'auto')

  return (
    <div className="flex flex-col gap-2" data-testid={`turn-mode-rules-${agentSlug}`}>
      <ul className="divide-y divide-border rounded-md border border-border" aria-label="Turn mode">
        {sorted.map((r) => (
          <li
            key={`${r.source}\u0000${r.actor}`}
            className="flex flex-wrap items-center gap-2 px-2 py-1.5 text-[12px]"
            data-testid={`turn-mode-rule-${r.source || 'any'}-${r.actor || 'anyone'}`}
          >
            <span className="min-w-0 flex-1 text-foreground">
              {r.actor ? <span className="font-mono">{r.actor}</span> : 'Anyone'}
              <span className="text-muted-foreground"> on {channel(r.source)}</span>
            </span>
            <select
              value={r.turn_mode}
              onChange={(e) => void save(r.source, r.actor, e.target.value as TurnMode)}
              aria-label={`Mode for ${r.actor || 'anyone'} on ${channel(r.source)}`}
              className={`rounded border border-input bg-input px-1.5 py-0.5 text-[11px] ${
                r.turn_mode === 'auto' ? 'font-medium text-primary' : 'text-foreground'
              }`}
            >
              <option value="manual">Manual</option>
              <option value="auto">Auto</option>
            </select>
            <button
              type="button"
              onClick={() => void remove(r)}
              aria-label={`Remove the mode rule for ${r.actor || 'anyone'} on ${channel(r.source)}`}
              className="text-muted-foreground hover:text-destructive"
            >
              ✕
            </button>
          </li>
        ))}
        {adding && (
          <AddTurnModeRule
            onCancel={() => setAdding(false)}
            onAdd={async (source, actor, mode) => {
              if (await save(source, actor, mode)) setAdding(false)
            }}
          />
        )}
        <li className="flex flex-wrap items-center gap-2 bg-muted/20 px-2 py-1.5" data-testid="turn-mode-default-row">
          <span className="min-w-0 flex-1 text-[12px] font-medium text-foreground">Everything else</span>
          <TurnModeToggle
            agentSlug={agentSlug}
            initialMode={initialTurnMode}
            workspace={workspace}
            compact
            onSaved={onSaved}
          />
        </li>
      </ul>

      <div className="flex flex-wrap items-center gap-3">
        {!adding && (
          <button
            type="button"
            onClick={() => setAdding(true)}
            className="w-fit rounded-md border border-input bg-input px-2 py-0.5 text-[11px] text-foreground-secondary hover:border-primary hover:text-primary"
            data-testid="turn-mode-rules-add-toggle"
          >
            + Add rule
          </button>
        )}
        {error && <span className="text-[11px] text-destructive">{error}</span>}
      </div>

      <p className="text-[11px] text-muted-foreground" data-testid="turn-mode-footer">
        <span className="text-foreground-secondary">Manual</span>: outbound actions wait for approval.{' '}
        <span className="text-foreground-secondary">Auto</span>: the agent reviews its own work and sends it. The first
        row that matches a turn decides. Setting Auto, or removing a Manual rule, is for the agent&apos;s owner and
        admins; no runner is involved.
        {namedAuto && (
          <span data-testid="turn-mode-verified-note">
            {' '}Auto for a named person holds only when the message is verified as theirs; anything else runs Manual.
          </span>
        )}
      </p>
    </div>
  )
}

function AddTurnModeRule({
  onAdd,
  onCancel,
}: {
  onAdd: (source: string, actor: string, mode: TurnMode) => void
  onCancel: () => void
}) {
  const [source, setSource] = useState<string>('')
  const [actor, setActor] = useState('')
  const [mode, setMode] = useState<TurnMode>('manual')
  const actorless = ACTORLESS_SOURCES.has(source)
  const who = actorless ? '' : actor.trim()
  const empty = !source && !who

  return (
    <li className="bg-primary/5 px-2 py-2" data-testid="turn-mode-add-row">
      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (!empty) onAdd(source, who, mode)
        }}
      >
        <input
          type="text"
          value={actorless ? '' : actor}
          disabled={actorless}
          onChange={(e) => setActor(e.target.value)}
          placeholder={actorless ? 'no person behind this channel' : 'anyone, or an address'}
          aria-label="Person"
          autoFocus
          className="min-w-40 flex-1 rounded border border-input bg-input px-1.5 py-0.5 font-mono text-[11px] text-foreground placeholder:text-muted-foreground disabled:opacity-50"
        />
        <span className="text-[11px] text-muted-foreground">on</span>
        <select
          value={source}
          onChange={(e) => setSource(e.target.value)}
          aria-label="Channel"
          className="rounded border border-input bg-input px-1 py-0.5 font-mono text-[11px] text-foreground"
        >
          <option value="">any channel</option>
          {ROUTABLE_SOURCES.map((s) => (
            <option key={s} value={s}>
              {SOURCE_LABEL[s] ?? s}
            </option>
          ))}
        </select>
        <select
          value={mode}
          onChange={(e) => setMode(e.target.value as TurnMode)}
          aria-label="Mode"
          className="rounded border border-input bg-input px-1 py-0.5 text-[11px] text-foreground"
        >
          <option value="manual">{MODE_LABEL.manual}</option>
          <option value="auto">{MODE_LABEL.auto}</option>
        </select>
        <button
          type="submit"
          disabled={empty}
          className="rounded-md bg-primary px-2.5 py-0.5 text-[11px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
          data-testid="turn-mode-add-submit"
        >
          Add rule
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
        >
          Cancel
        </button>
        {empty && <span className="text-[11px] text-muted-foreground">Name a person, a channel, or both.</span>}
      </form>
    </li>
  )
}
