import { useEffect, useRef, useState, type JSX, type ReactNode } from 'react'
import {
  getAgentRunnerRules,
  putAgentRunnerRules,
  type AgentRunnerRuleOut,
  type RoutableSource,
  type RuleTurnMode,
  type TurnMode,
} from '@/api/agents'
import { listRunners, type RunnerOut } from '@/api/harness'
import { RunnerAssignments } from '@/components/agents/RunnerAssignments'
import {
  ACTORLESS_SOURCES,
  ROUTABLE_SOURCES,
  SOURCE_LABEL,
  asRuleMode,
  groupRules,
  hasRule,
  inheritedMode,
  nextRulesForActor,
  nextRulesForAdd,
  nextRulesForMode,
  nextRulesForRemove,
  nextRulesForRunnerAdd,
  nextRulesForRunnerMove,
  nextRulesForRunnerRemove,
  nextRulesForStrict,
  ruleKey,
  toRows,
  type GroupedRule,
  type RuleRow,
} from '@/components/agents/RunnerSourceRules'
import { TurnModeToggle } from '@/components/agents/TurnModeToggle'

// ONE TABLE for "where does this agent's work run, and how" (spec 2026-09-23).
//
// It used to be three controls that each answered part of the question: a Turn
// mode toggle, a "Default order" chip row, and beneath it an "Except when the
// work comes from" list whose precedence was explained in a footnote. Once a
// rule could set the MODE as well as the box, the three stopped being separable
// — "Beth's email → cloud, auto" is one decision — so they are rows of one table
// now, in the order they are evaluated: the specific rules first, the agent's
// defaults last as "Everything else". Reading it top to bottom IS the precedence,
// which is what let the footnote shrink to the two cases the table cannot show.
//
// Layout is a CSS grid under a container query rather than a <table>: the same
// component renders in the Settings page (wide) and in the supervisor's runner
// drill-down (a phone-width panel), and below the breakpoint each row stacks
// into labelled lines instead of scrolling sideways.

const GRID =
  '@3xl:grid @3xl:grid-cols-[5.5rem_minmax(0,10rem)_minmax(0,1fr)_auto_8.5rem_1rem] @3xl:items-center @3xl:gap-x-3'

const MODE_LABEL: Record<'manual' | 'auto', string> = { manual: 'Manual', auto: 'Auto' }

function Cell({ label, children, className = '' }: { label: string; children: ReactNode; className?: string }) {
  // The label is for the stacked (narrow) layout only; the header row names the
  // columns on the wide one.
  return (
    <div role="cell" className={`flex min-w-0 items-center gap-2 @3xl:block ${className}`}>
      <span className="w-20 shrink-0 text-[10px] uppercase tracking-wide text-muted-foreground @3xl:hidden">
        {label}
      </span>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  )
}

function Segmented<T extends string>({
  value, options, onChange, label,
}: {
  value: T
  options: { value: T; label: string }[]
  onChange: (v: T) => void
  label: string
}) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-md border border-border bg-input p-0.5">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="radio"
          aria-checked={value === o.value}
          onClick={() => value !== o.value && onChange(o.value)}
          className={`rounded px-2 py-0.5 text-[11px] font-medium transition-colors ${
            value === o.value ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

// A rule's mode as a select, because it has three states and the third one
// ("inherit") has to say WHAT it inherits — a segmented control cannot hold that.
function RuleModeSelect({
  value, inherited, onChange, label,
}: {
  value: RuleTurnMode
  inherited: 'manual' | 'auto'
  onChange: (v: RuleTurnMode) => void
  label: string
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(asRuleMode(e.target.value))}
      aria-label={label}
      className={`w-full rounded border border-input bg-input px-1.5 py-0.5 text-[11px] ${
        value === 'auto' ? 'font-medium text-primary' : value ? 'text-foreground' : 'text-muted-foreground'
      }`}
    >
      <option value="">As below ({MODE_LABEL[inherited]})</option>
      <option value="manual">Manual</option>
      <option value="auto">Auto</option>
    </select>
  )
}

export function AgentRouting({
  agentSlug,
  initialTurnMode,
}: {
  agentSlug: string
  initialTurnMode: TurnMode
}): JSX.Element {
  const [rules, setRules] = useState<AgentRunnerRuleOut[] | null>(null)
  const [fleet, setFleet] = useState<readonly RunnerOut[]>([])
  const [agentMode, setAgentMode] = useState<TurnMode>(initialTurnMode)
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)

  // Optimistic commits, as RunnerAssignments does them: a ref that keeps pace with
  // local state so a fast second edit composes on the first, and a sequence so an
  // out-of-order response cannot undo a newer edit.
  const rulesRef = useRef<AgentRunnerRuleOut[]>([])
  const apply = (next: AgentRunnerRuleOut[]) => {
    rulesRef.current = next
    setRules(next)
  }
  const seqRef = useRef(0)

  useEffect(() => {
    let cancelled = false
    setRules(null)
    rulesRef.current = []
    setError(null)
    Promise.all([getAgentRunnerRules(agentSlug), listRunners()])
      .then(([r, f]) => {
        if (cancelled) return
        apply(r)
        setFleet(f)
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setError(e instanceof Error ? e.message : 'Failed to load')
        apply([])
      })
    return () => {
      cancelled = true
    }
  }, [agentSlug])

  const commit = async (next: RuleRow[], prev: AgentRunnerRuleOut[]) => {
    const mySeq = ++seqRef.current
    // Patch names/state in from the fleet so a new rule renders at once rather
    // than flashing empty until the PUT returns; flattened back to one row per
    // runner, which is the shape the server returns.
    apply(
      next.flatMap((r) =>
        r.runnerIds.map((id, rank) => {
          const existing = prev.find((p) => ruleKey(p) === ruleKey(r) && p.runner_id === id)
          const f = fleet.find((x) => x.id === id)
          return {
            source: r.source,
            actor: r.actor,
            rank,
            runner_id: id,
            runner_name: f?.name ?? existing?.runner_name ?? id,
            kind: f?.kind ?? existing?.kind ?? '',
            strict: r.strict,
            online: f ? f.status === 'online' : (existing?.online ?? false),
            ready: f?.ready ?? existing?.ready ?? false,
            enabled: true,
            queued_count: existing?.queued_count ?? 0,
            turn_mode: r.turnMode,
          } as AgentRunnerRuleOut
        }),
      ),
    )
    setError(null)
    try {
      const saved = await putAgentRunnerRules(agentSlug, next)
      if (seqRef.current !== mySeq) return
      apply(saved)
    } catch (e: unknown) {
      if (seqRef.current !== mySeq) return
      apply(prev)
      setError(e instanceof Error ? e.message : 'Failed to save')
    }
  }

  const mutate = (fn: (rows: RuleRow[]) => RuleRow[]) => {
    const prev = rulesRef.current
    void commit(fn(toRows(prev)), prev)
  }

  if (rules === null) {
    return <div className="h-24 w-full animate-pulse rounded-md bg-muted" data-testid="agent-routing-loading" />
  }

  const grouped = groupRules(rules)

  return (
    <div className="@container flex flex-col gap-2" data-testid={`runner-rules-${agentSlug}`}>
      <div role="table" aria-label="Routing" className="divide-y divide-border rounded-md border border-border">
        <div role="row" className={`hidden bg-muted/40 px-2 py-1 text-[10px] uppercase tracking-wide text-muted-foreground ${GRID}`}>
          <span role="columnheader">Work</span>
          <span role="columnheader">From</span>
          <span role="columnheader">Runs on, in order</span>
          <span role="columnheader">If all are down</span>
          <span role="columnheader">Mode</span>
          <span />
        </div>

        {grouped.map((g) => (
          <RuleRowView
            key={ruleKey(g)}
            g={g}
            fleet={fleet}
            inherited={inheritedMode(g, grouped, agentMode)}
            mutate={mutate}
          />
        ))}

        {adding && (
          <AddRuleRow
            fleet={fleet}
            rules={grouped}
            agentMode={agentMode}
            onCancel={() => setAdding(false)}
            onAdd={(source, actor, runnerId, turnMode) => {
              setAdding(false)
              mutate((rows) => nextRulesForAdd(rows, source, runnerId, { actor, turnMode }))
            }}
          />
        )}

        {/* The agent's own defaults — last because they are what every row
            above falls back to. Its mode IS the agent's turn mode. */}
        <div role="row" className={`flex flex-col gap-1.5 bg-muted/20 px-2 py-2 ${GRID}`} data-testid="routing-default-row">
          <Cell label="Work" className="@3xl:col-span-2">
            <span className="text-[12px] font-medium text-foreground">Everything else</span>
          </Cell>
          <Cell label="Runs on">
            <RunnerAssignments agentSlug={agentSlug} fleet={fleet} />
          </Cell>
          <Cell label="If all down">
            <span className="text-[11px] text-muted-foreground">Waits</span>
          </Cell>
          <Cell label="Mode">
            <TurnModeToggle agentSlug={agentSlug} initialMode={initialTurnMode} compact onChange={setAgentMode} />
          </Cell>
          <span />
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        {!adding && (
          <button
            type="button"
            onClick={() => setAdding(true)}
            disabled={fleet.length === 0}
            className="w-fit rounded-md border border-input bg-input px-2 py-0.5 text-[11px] text-foreground-secondary hover:border-primary hover:text-primary disabled:opacity-40"
            data-testid="runner-rules-add-toggle"
          >
            + Add rule
          </button>
        )}
        {error && <span className="text-[11px] text-destructive">{error}</span>}
      </div>

      <p className="text-[11px] leading-relaxed text-muted-foreground">
        Rows are checked top to bottom. The first that matches a turn picks its runners and its
        mode. <span className="text-foreground-secondary">Fall back</span> passes the turn to the
        rows below when none of the row&apos;s runners is available;{' '}
        <span className="text-foreground-secondary">Wait</span> holds it until one is.{' '}
        <span className="text-foreground-secondary">Manual</span>: outbound actions wait for
        approval. <span className="text-foreground-secondary">Auto</span>: the agent reviews its
        own work and sends it.
      </p>
      {/* The two cases the table cannot show, and the one place `enabled` means
          two things: a runner switched off under Everything else still takes
          the work of any rule that names it, because the rule is its own row. */}
      <p className="text-[10px] text-foreground-subtle">
        A turn pinned to a runner, or a chat already live on one, skips this table. A rule uses its
        runners even when they are switched off under Everything else.
      </p>
    </div>
  )
}

function RuleRowView({
  g, fleet, inherited, mutate,
}: {
  g: GroupedRule
  fleet: readonly RunnerOut[]
  inherited: 'manual' | 'auto'
  mutate: (fn: (rows: RuleRow[]) => RuleRow[]) => void
}) {
  const key = ruleKey(g)
  const testId = g.actor ? `${g.source}-${g.actor}` : g.source
  const source = SOURCE_LABEL[g.source] ?? g.source
  const unused = fleet.filter((f) => !g.runners.some((r) => r.runner_id === f.id))
  const actorless = ACTORLESS_SOURCES.has(g.source)
  const effective = g.turnMode || inherited

  return (
    <div role="row" className="px-2 py-2" data-testid={`runner-rule-${testId}`}>
      <div className={`flex flex-col gap-1.5 ${GRID}`}>
        <Cell label="Work">
          <span className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 font-mono text-[11px] text-primary">
            {source}
          </span>
        </Cell>

        <Cell label="From">
          {actorless ? (
            // A schedule has no sender; offering a field would invite a rule
            // that can never match.
            <span className="text-[11px] text-muted-foreground">—</span>
          ) : (
            // Free text, placeholder `anyone`: paste what a mail client shows; the
            // server reduces "Name <addr>" to the bare address it routes on.
            <input
              type="text"
              defaultValue={g.actor}
              placeholder="anyone"
              onBlur={(e) => {
                const v = e.target.value.trim()
                if (v !== g.actor) mutate((rows) => nextRulesForActor(rows, key, v))
              }}
              aria-label={`Sender for the ${source} rule`}
              className="w-full rounded border border-input bg-input px-1.5 py-0.5 font-mono text-[11px] text-foreground placeholder:text-muted-foreground"
            />
          )}
        </Cell>

        <Cell label="Runs on">
          <div className="flex flex-wrap items-center gap-1">
            {g.runners.map((r, i) => (
              <span
                key={r.runner_id}
                className="flex min-w-0 max-w-full items-center gap-1 whitespace-nowrap rounded border border-input bg-input px-1.5 py-0.5 text-[11px]"
                data-testid={`rule-runner-${testId}-${r.runner_name}`}
              >
                {g.runners.length > 1 && (
                  <span className="text-[10px] font-semibold text-muted-foreground">{i + 1}</span>
                )}
                <span className={r.online ? 'text-success' : 'text-muted-foreground'} title={r.online ? 'online' : 'offline'}>
                  ●
                </span>
                <span className="min-w-0 truncate font-mono" title={r.runner_name}>{r.runner_name}</span>
                {g.runners.length > 1 && (
                  <>
                    <button
                      type="button"
                      onClick={() => mutate((rows) => nextRulesForRunnerMove(rows, key, i, -1))}
                      disabled={i === 0}
                      aria-label={`Move ${r.runner_name} up in the ${source} rule`}
                      className="text-muted-foreground hover:text-primary disabled:opacity-30"
                    >
                      ↑
                    </button>
                    <button
                      type="button"
                      onClick={() => mutate((rows) => nextRulesForRunnerMove(rows, key, i, 1))}
                      disabled={i === g.runners.length - 1}
                      aria-label={`Move ${r.runner_name} down in the ${source} rule`}
                      className="text-muted-foreground hover:text-primary disabled:opacity-30"
                    >
                      ↓
                    </button>
                  </>
                )}
                <button
                  type="button"
                  onClick={() => mutate((rows) => nextRulesForRunnerRemove(rows, key, r.runner_id))}
                  aria-label={`Remove ${r.runner_name} from the ${source} rule`}
                  title={g.runners.length === 1 ? 'removes the rule' : undefined}
                  className="text-muted-foreground hover:text-destructive"
                >
                  ✕
                </button>
              </span>
            ))}
            {unused.length > 0 && (
              <select
                value=""
                onChange={(e) => {
                  if (e.target.value) mutate((rows) => nextRulesForRunnerAdd(rows, key, e.target.value))
                }}
                aria-label={`Add a runner to the ${source} rule`}
                title="Add a runner to this rule"
                className="w-9 rounded border border-input bg-input px-1 py-0.5 text-[11px] text-foreground-secondary"
              >
                <option value="">+</option>
                {unused.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.name}
                  </option>
                ))}
              </select>
            )}
          </div>
        </Cell>

        <Cell label="If all down">
          <Segmented
            label={`When the ${source} rule's runners are all down`}
            value={g.strict ? 'wait' : 'fall'}
            options={[
              { value: 'fall', label: 'Fall back' },
              { value: 'wait', label: 'Wait' },
            ]}
            onChange={() => mutate((rows) => nextRulesForStrict(rows, key))}
          />
        </Cell>

        <Cell label="Mode">
          <RuleModeSelect
            value={g.turnMode}
            inherited={inherited}
            label={`Mode for the ${source} rule`}
            onChange={(m) => mutate((rows) => nextRulesForMode(rows, key, m))}
          />
        </Cell>

        {/* Deletable outright, unlike a default-order runner (which toggles):
            a rule is cheap to re-add. */}
        <button
          type="button"
          onClick={() => mutate((rows) => nextRulesForRemove(rows, key))}
          aria-label={`Remove the ${source} rule`}
          className="self-end text-muted-foreground hover:text-destructive @3xl:self-auto"
        >
          ✕
        </button>
      </div>

      {/* An auto that names a person holds only when THIS message proves it is
          from them — the server enforces it; the operator must not have to
          discover it from a turn that ran manual. */}
      {g.actor && effective === 'auto' && (
        <p className="mt-1 text-[11px] text-muted-foreground" data-testid={`runner-rule-verified-${testId}`}>
          Auto only when the message is verified as coming from {g.actor}. Anything that cannot
          prove it runs Manual.
        </p>
      )}

      {/* Waiting is the toggle working; waiting SILENTLY is the failure. Only
          when EVERY runner is down, since naming two exists so one asleep is
          not a parked queue. */}
      {g.parked && (
        <p className="mt-1 text-[11px] text-warning" data-testid={`runner-rule-parked-${testId}`}>
          ⚠ {g.runners.map((r) => r.runner_name).join(' and ')} {g.runners.length > 1 ? 'are' : 'is'} offline
          {g.queuedCount > 0
            ? ` — ${g.queuedCount} ${source} turn${g.queuedCount === 1 ? '' : 's'}${g.actor ? ` from ${g.actor}` : ''} waiting, and will keep waiting.`
            : ' — this work will wait until one returns.'}
        </p>
      )}
    </div>
  )
}

function AddRuleRow({
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
