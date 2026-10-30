// The rows of the routing table: a rule's row, the add-a-rule row, and the cells and mode select they share. The table and its state are `AgentRouting`.
import { type ReactNode } from 'react'
import {
  type RuleTurnMode,
  } from '@/api/agents'
import { Segmented } from 'canopy-ui/ui'
import { type RunnerOut } from '@/api/harness'
import { SensitiveDataBadge, hasSensitiveData } from '@/components/agents/SensitiveDataBadge'
import {
  ACTORLESS_SOURCES,
  ROUTABLE_SOURCES,
  SOURCE_LABEL,
  asRuleMode,
  forEachKey,
  hasRule,
  nextRulesForActor,
  nextRulesForCopy,
  nextRulesForMode,
  nextRulesForRemove,
  nextRulesForRunnerAdd,
  nextRulesForRunnerMove,
  nextRulesForRunnerRemove,
  nextRulesForStrict,
  ruleKey,
  type GroupedRule,
  type MergedRule,
  type RuleRow,
} from '@/components/agents/RunnerSourceRules'

export const GRID =
  '@3xl:grid @3xl:grid-cols-[5.5rem_minmax(0,10rem)_minmax(0,1fr)_auto_8.5rem_1rem] @3xl:items-center @3xl:gap-x-3'

export const MODE_LABEL: Record<'manual' | 'auto', string> = { manual: 'Manual', auto: 'Auto' }

export function Cell({ label, children, className = '' }: { label: string; children: ReactNode; className?: string }) {
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

// A rule's mode as a select, because it has three states and the third one
// ("inherit") has to say WHAT it inherits — a segmented control cannot hold that.
export function RuleModeSelect({
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

export function RuleRowView({
  m, all, fleet, mutate,
}: {
  m: MergedRule
  /** Every rule, so the add-a-kind picker can leave out (kind, sender) pairs that exist. */
  all: readonly GroupedRule[]
  fleet: readonly RunnerOut[]
  mutate: (fn: (rows: RuleRow[]) => RuleRow[]) => void
}) {
  // Every edit to the row is applied to each rule under it.
  const each = (fn: (rows: RuleRow[], key: string) => RuleRow[]) => mutate((rows) => forEachKey(rows, m.keys, fn))
  const kinds = m.sources.join('+')
  const testId = m.actor ? `${kinds}-${m.actor}` : kinds
  const source = m.sources.map((s) => SOURCE_LABEL[s] ?? s).join(', ')
  const unused = fleet.filter((f) => !m.runners.some((r) => r.runner_id === f.id))
  // A schedule has no sender: the sender field shows only when some kind here has one.
  const actorless = m.sources.every((s) => ACTORLESS_SOURCES.has(s))
  const senderKeys = m.rules.filter((r) => !ACTORLESS_SOURCES.has(r.source)).map(ruleKey)
  const addable = ROUTABLE_SOURCES.filter(
    (s) => !m.sources.includes(s) && !hasRule(all, s, m.actor) && !(m.actor && ACTORLESS_SOURCES.has(s)),
  )

  return (
    <div role="row" className="px-2 py-2" data-testid={`runner-rule-${testId}`}>
      <div className={`flex flex-col gap-1.5 ${GRID}`}>
        <Cell label="Work">
          <div className="flex flex-wrap items-center gap-1">
            {m.rules.map((r) => {
              const label = SOURCE_LABEL[r.source] ?? r.source
              return (
                <span
                  key={r.source}
                  className="inline-flex items-center gap-1 whitespace-nowrap rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 font-mono text-[11px] text-primary"
                >
                  {label}
                  {m.rules.length > 1 && (
                    <button
                      type="button"
                      onClick={() => mutate((rows) => nextRulesForRemove(rows, ruleKey(r)))}
                      aria-label={`Stop routing ${label} by this rule`}
                      className="text-primary/60 hover:text-destructive"
                    >
                      ✕
                    </button>
                  )}
                </span>
              )
            })}
            {addable.length > 0 && (
              <select
                value=""
                onChange={(e) => {
                  if (e.target.value) mutate((rows) => nextRulesForCopy(rows, m.keys[0], e.target.value))
                }}
                aria-label={`Add a kind of work to the ${source} rule`}
                title="Route another kind of work the same way"
                className="w-9 rounded border border-input bg-input px-1 py-0.5 text-[11px] text-foreground-secondary"
              >
                <option value="">+</option>
                {addable.map((s) => (
                  <option key={s} value={s}>
                    {SOURCE_LABEL[s] ?? s}
                  </option>
                ))}
              </select>
            )}
          </div>
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
              defaultValue={m.actor}
              placeholder="anyone"
              onBlur={(e) => {
                const v = e.target.value.trim()
                if (v !== m.actor)
                  mutate((rows) => forEachKey(rows, senderKeys, (acc, key) => nextRulesForActor(acc, key, v)))
              }}
              aria-label={`Sender for the ${source} rule`}
              className="w-full rounded border border-input bg-input px-1.5 py-0.5 font-mono text-[11px] text-foreground placeholder:text-muted-foreground"
            />
          )}
        </Cell>

        <Cell label="Runs on">
          <div className="flex flex-wrap items-center gap-1">
            {m.runners.map((r, i) => (
              <span
                key={r.runner_id}
                className="flex min-w-0 max-w-full items-center gap-1 whitespace-nowrap rounded border border-input bg-input px-1.5 py-0.5 text-[11px]"
                data-testid={`rule-runner-${testId}-${r.runner_name}`}
              >
                {m.runners.length > 1 && (
                  <span className="text-[10px] font-semibold text-muted-foreground">{i + 1}</span>
                )}
                <span className={r.online ? 'text-success' : 'text-muted-foreground'} title={r.online ? 'online' : 'offline'}>
                  ●
                </span>
                <span className="min-w-0 truncate font-mono" title={r.runner_name}>{r.runner_name}</span>
                {hasSensitiveData(fleet.find((f) => f.id === r.runner_id)) && (
                  <SensitiveDataBadge testId={`sensitive-data-badge-${r.runner_name}`} />
                )}
                {m.runners.length > 1 && (
                  <>
                    <button
                      type="button"
                      onClick={() => each((rows, key) => nextRulesForRunnerMove(rows, key, i, -1))}
                      disabled={i === 0}
                      aria-label={`Move ${r.runner_name} up in the ${source} rule`}
                      className="text-muted-foreground hover:text-primary disabled:opacity-30"
                    >
                      ↑
                    </button>
                    <button
                      type="button"
                      onClick={() => each((rows, key) => nextRulesForRunnerMove(rows, key, i, 1))}
                      disabled={i === m.runners.length - 1}
                      aria-label={`Move ${r.runner_name} down in the ${source} rule`}
                      className="text-muted-foreground hover:text-primary disabled:opacity-30"
                    >
                      ↓
                    </button>
                  </>
                )}
                <button
                  type="button"
                  onClick={() => each((rows, key) => nextRulesForRunnerRemove(rows, key, r.runner_id))}
                  aria-label={`Remove ${r.runner_name} from the ${source} rule`}
                  title={m.runners.length === 1 ? 'removes the rule' : undefined}
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
                  if (e.target.value) each((rows, key) => nextRulesForRunnerAdd(rows, key, e.target.value))
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
            size="xs"
            label={`When the ${source} rule's runners are all down`}
            value={m.strict ? 'wait' : 'fall'}
            options={[
              { value: 'fall', label: 'Fall back' },
              { value: 'wait', label: 'Wait' },
            ]}
            onChange={() => each((rows, key) => nextRulesForStrict(rows, key))}
          />
        </Cell>

        <Cell label="Mode">
          <RuleModeSelect
            value={m.turnMode}
            inherited={m.inherited}
            label={`Mode for the ${source} rule`}
            onChange={(mode) => each((rows, key) => nextRulesForMode(rows, key, mode))}
          />
        </Cell>

        {/* Deletable outright, unlike a default-order runner (which toggles):
            a rule is cheap to re-add. */}
        <button
          type="button"
          onClick={() => each((rows, key) => nextRulesForRemove(rows, key))}
          aria-label={`Remove the ${source} rule`}
          className="self-end text-muted-foreground hover:text-destructive @3xl:self-auto"
        >
          ✕
        </button>
      </div>

      {/* Waiting is the toggle working; waiting SILENTLY is the failure. Only
          when EVERY runner is down, since naming two exists so one asleep is
          not a parked queue. */}
      {m.parked && (
        <p className="mt-1 text-[11px] text-warning" data-testid={`runner-rule-parked-${testId}`}>
          ⚠ {m.runners.map((r) => r.runner_name).join(' and ')} {m.runners.length > 1 ? 'are' : 'is'} offline
          {m.queuedCount > 0
            ? ` — ${m.queuedCount} ${source} turn${m.queuedCount === 1 ? '' : 's'}${m.actor ? ` from ${m.actor}` : ''} waiting, and will keep waiting.`
            : ' — this work will wait until one returns.'}
        </p>
      )}
    </div>
  )
}

