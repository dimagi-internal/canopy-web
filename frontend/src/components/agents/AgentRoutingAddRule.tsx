// The add-a-rule row of the routing table (`AgentRouting`).
import { useState, } from 'react'
import {
  type RoutableSource,
  type RuleTurnMode,
  } from '@/api/agents'
import { type RunnerOut } from '@/api/harness'
import {
  ACTORLESS_SOURCES,
  ROUTABLE_SOURCES,
  SOURCE_LABEL,
  hasRule,
  inheritedMode,
  type GroupedRule,
  } from '@/components/agents/RunnerSourceRules'

import { GRID, Cell, RuleModeSelect } from './AgentRoutingRules'

export function AddRuleRow({
  fleet, rules, agentMode, onAdd, onCancel,
}: {
  fleet: readonly RunnerOut[]
  rules: readonly GroupedRule[]
  agentMode: 'manual' | 'auto'
  onAdd: (source: string, actor: string, runnerId: string, turnMode: RuleTurnMode) => void
  onCancel: () => void
}) {
  const [source, setSource] = useState<RoutableSource>('email')
  const [actor, setActor] = useState('')
  const [runnerId, setRunnerId] = useState(fleet[0]?.id ?? '')
  const [turnMode, setTurnMode] = useState<RuleTurnMode>('')
  const actorless = ACTORLESS_SOURCES.has(source)
  const who = actorless ? '' : actor.trim()
  const duplicate = hasRule(rules, source, who)
  const label = SOURCE_LABEL[source] ?? source

  const submit = () => {
    if (duplicate || !runnerId) return
    onAdd(source, who, runnerId, turnMode)
  }

  return (
    <div role="row" className="bg-primary/5 px-2 py-2" data-testid="routing-add-row">
      <form
        className={`flex flex-col gap-1.5 ${GRID}`}
        onSubmit={(e) => {
          e.preventDefault()
          submit()
        }}
      >
        <Cell label="Work">
          <select
            value={source}
            onChange={(e) => setSource(e.target.value as RoutableSource)}
            aria-label="Work"
            className="w-full rounded border border-input bg-input px-1 py-0.5 font-mono text-[11px] text-foreground"
          >
            {ROUTABLE_SOURCES.map((s) => (
              <option key={s} value={s}>
                {SOURCE_LABEL[s] ?? s}
              </option>
            ))}
          </select>
        </Cell>
        <Cell label="From">
          {actorless ? (
            <span className="text-[11px] text-muted-foreground">—</span>
          ) : (
            <input
              type="text"
              value={actor}
              onChange={(e) => setActor(e.target.value)}
              placeholder="anyone, or an address"
              aria-label="Sender"
              autoFocus
              className="w-full rounded border border-input bg-input px-1.5 py-0.5 font-mono text-[11px] text-foreground placeholder:text-muted-foreground"
            />
          )}
        </Cell>
        <Cell label="Runs on">
          <select
            value={runnerId}
            onChange={(e) => setRunnerId(e.target.value)}
            aria-label="Runner"
            className="rounded border border-input bg-input px-1 py-0.5 text-[11px] text-foreground"
          >
            {fleet.map((f) => (
              <option key={f.id} value={f.id}>
                {f.name}
              </option>
            ))}
          </select>
        </Cell>
        <Cell label="If all down">
          <span className="text-[11px] text-muted-foreground">Fall back</span>
        </Cell>
        <Cell label="Mode">
          <RuleModeSelect
            value={turnMode}
            inherited={who ? inheritedMode({ source, actor: who }, rules, agentMode) : agentMode}
            label="Mode"
            onChange={setTurnMode}
          />
        </Cell>
        <span />
      </form>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={submit}
          disabled={duplicate || !runnerId}
          className="rounded-md bg-primary px-2.5 py-0.5 text-[11px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40"
          data-testid="routing-add-submit"
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
        {duplicate && (
          <span className="text-[11px] text-warning">
            {label} from {who || 'anyone'} already has a row. Edit it above.
          </span>
        )}
      </div>
    </div>
  )
}
