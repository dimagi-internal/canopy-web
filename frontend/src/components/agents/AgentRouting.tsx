import { useEffect, useRef, useState, type JSX, } from 'react'
import {
  getAgentRunnerRules,
  saveAgentRunnerRules,
  type AgentRunnerRuleOut,
  type TurnMode,
} from '@/api/agents'
import { listRunners, type RunnerOut } from '@/api/harness'
import { AgentDefaultRunners } from '@/components/agents/AgentDefaultRunners'
import {
  groupRules,
  mergeRules,
  nextRulesForAdd,
  ruleKey,
  toRows,
  type RuleRow,
} from '@/components/agents/RunnerSourceRules'
import { TurnModeToggle } from '@/components/agents/TurnModeToggle'
import { GRID, Cell, RuleRowView } from './AgentRoutingRules'
import { AddRuleRow } from './AgentRoutingAddRule'

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

export function AgentRouting({
  agentSlug,
  initialTurnMode,
  workspace,
  onSaved,
  agentName,
}: {
  agentSlug: string
  agentName?: string
  /** The agent's workspace, when the page is under another (the fleet map at a
   *  parent, the supervisor). Its routes resolve the agent within one tenant. */
  workspace?: string
  initialTurnMode: TurnMode
  /** A rule, the default order or the mode was saved — for a host that draws
   *  the routing elsewhere too (the fleet map) and must redraw it. */
  onSaved?: () => void
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
    Promise.all([getAgentRunnerRules(agentSlug, workspace), listRunners()])
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
  }, [agentSlug, workspace])

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
      // One rule at a time: only what this edit changed is sent (#1143).
      const saved = await saveAgentRunnerRules(agentSlug, prev, next, workspace)
      if (seqRef.current !== mySeq) return
      apply(saved)
      onSaved?.()
    } catch (e: unknown) {
      if (seqRef.current !== mySeq) return
      setError(e instanceof Error ? e.message : 'Failed to save')
      // Rules save one by one, so a failure can land part-way: show what the
      // server now holds rather than assuming nothing was written.
      try {
        const now = await getAgentRunnerRules(agentSlug, workspace)
        if (seqRef.current === mySeq) apply(now)
      } catch {
        if (seqRef.current === mySeq) apply(prev)
      }
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
  const merged = mergeRules(grouped, agentMode)
  // Said once, here, rather than under every row it applies to (#1314).
  const namedAuto = merged.some((m) => m.actor && (m.turnMode || m.inherited) === 'auto')

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

        {merged.map((m) => (
          <RuleRowView key={m.keys.join('|')} m={m} all={grouped} fleet={fleet} mutate={mutate} />
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
            <AgentDefaultRunners
              agentSlug={agentSlug}
              agentName={agentName ?? agentSlug}
              workspace={workspace}
              fleet={fleet}
              onSaved={onSaved}
            />
          </Cell>
          <Cell label="If all down">
            <span className="text-[11px] text-muted-foreground">Waits</span>
          </Cell>
          <Cell label="Mode">
            <TurnModeToggle
              agentSlug={agentSlug}
              initialMode={initialTurnMode}
              workspace={workspace}
              compact
              onChange={setAgentMode}
              onSaved={onSaved}
            />
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

      {/* One line, and the detail behind a disclosure (#1314): it was two
          paragraphs under the table, read once and skipped ever after. */}
      <p className="text-[11px] text-muted-foreground" data-testid="routing-footer">
        The first row that matches a turn picks its runners and mode.
        {namedAuto && (
          <span data-testid="routing-verified-note">
            {' '}Auto for a named sender holds only when the message is verified as theirs; anything else runs Manual.
          </span>
        )}
      </p>
      <details className="text-[11px] text-muted-foreground">
        <summary className="w-fit cursor-pointer text-primary hover:underline">How routing works</summary>
        <p className="mt-1 leading-relaxed">
          Rows are checked top to bottom. <span className="text-foreground-secondary">Fall back</span> passes
          the turn to the rows below when none of the row&apos;s runners is available;{' '}
          <span className="text-foreground-secondary">Wait</span> holds it until one is.{' '}
          <span className="text-foreground-secondary">Manual</span>: outbound actions wait for approval.{' '}
          <span className="text-foreground-secondary">Auto</span>: the agent reviews its own work and sends it.
          {/* The cases the table cannot show, and the one place `enabled`
              means two things: a runner switched off under Everything else
              still takes the work of any rule that names it. */}{' '}
          A turn pinned to a runner, or a chat already live on one, skips this table. A rule uses its runners
          even when they are switched off under Everything else.
        </p>
      </details>
    </div>
  )
}

