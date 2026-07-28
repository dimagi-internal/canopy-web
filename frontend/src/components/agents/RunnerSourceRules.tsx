import { useEffect, useRef, useState, type JSX } from 'react'
import {
  getAgentRunnerRules,
  putAgentRunnerRules,
  type AgentRunnerRuleOut,
  type RoutableSource,
} from '@/api/agents'
import { type RunnerOut } from '@/api/harness'

// The per-source exception list that sits under an agent's DEFAULT runner order
// (see RunnerAssignments). A rule is one priority runner plus a strict toggle —
// deliberately not a second ordered list: ordering already exists once, in the
// default list, and a source only needs to say "prefer this box" and optionally
// "and nowhere else".
//
// Mirrors RunnerAssignments' commit machinery: optimistic local state, a ref that
// keeps pace with it so a fast second click composes on top of the first, and a
// monotonic sequence so an out-of-order response can't undo a newer edit.
//
// The fleet arrives as a PROP rather than being fetched here: the parent already
// holds it, and the Runners tab renders one of these per agent.

// Ordered as the picker offers them: the sources with real producers first.
// `api` is last because it is the catch-all, and `slack` is reserved for a
// producer that does not exist yet (a rule on it is inert, not harmful).
export const ROUTABLE_SOURCES: readonly RoutableSource[] = [
  'ace_web',
  'email',
  'canopy_scheduler',
  'canopy_web_chat',
  'slack',
  'api',
]

const SOURCE_LABEL: Record<string, string> = {
  ace_web: 'ace-web',
  email: 'email',
  canopy_scheduler: 'scheduler',
  canopy_web_chat: 'canopy chat',
  slack: 'slack',
  api: 'api (unclassified)',
}

export type RuleRow = { source: string; runnerId: string; strict: boolean }

function toRows(rules: readonly AgentRunnerRuleOut[]): RuleRow[] {
  return rules.map((r) => ({ source: r.source, runnerId: r.runner_id, strict: r.strict }))
}

// Pure list transforms, extracted so they unit-test without a React renderer
// (see RunnerSourceRules.test.tsx). None of them mutates its input — the commit
// lane keeps a `prev` snapshot to revert to on failure.
export function nextRulesForAdd(
  rules: readonly RuleRow[], source: string, runnerId: string,
): RuleRow[] {
  // strict=false by default: a new rule prefers a box without parking the queue
  // when that box is down. Opting into "only" should be a deliberate click.
  return [...rules, { source, runnerId, strict: false }]
}

export function nextRulesForRunner(
  rules: readonly RuleRow[], source: string, runnerId: string,
): RuleRow[] {
  return rules.map((r) => (r.source === source ? { ...r, runnerId } : r))
}

export function nextRulesForStrict(rules: readonly RuleRow[], source: string): RuleRow[] {
  return rules.map((r) => (r.source === source ? { ...r, strict: !r.strict } : r))
}

export function nextRulesForRemove(rules: readonly RuleRow[], source: string): RuleRow[] {
  return rules.filter((r) => r.source !== source)
}

export function availableSources(rules: readonly RuleRow[]): RoutableSource[] {
  const taken = new Set(rules.map((r) => r.source))
  return ROUTABLE_SOURCES.filter((s) => !taken.has(s))
}

export function RunnerSourceRules(
  { agentSlug, fleet }: { agentSlug: string; fleet: readonly RunnerOut[] },
): JSX.Element {
  const [rules, setRules] = useState<AgentRunnerRuleOut[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)

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
    getAgentRunnerRules(agentSlug)
      .then((r) => {
        if (!cancelled) apply(r)
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
    // Optimistic: patch names/state from the fleet list so a fresh rule renders
    // immediately rather than flashing empty until the PUT returns.
    apply(
      next.map((r) => {
        const existing = prev.find((p) => p.source === r.source)
        const f = fleet.find((x) => x.id === r.runnerId)
        return {
          source: r.source,
          runner_id: r.runnerId,
          runner_name: f?.name ?? existing?.runner_name ?? r.runnerId,
          kind: f?.kind ?? existing?.kind ?? '',
          strict: r.strict,
          online: f ? f.status === 'online' : (existing?.online ?? false),
          ready: f?.ready ?? existing?.ready ?? false,
          enabled: true,
          queued_count: existing?.queued_count ?? 0,
        } as AgentRunnerRuleOut
      }),
    )
    setError(null)
    try {
      const saved = await putAgentRunnerRules(agentSlug, next)
      if (seqRef.current !== mySeq) return // superseded by a newer commit
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
    return (
      <div
        className="h-6 w-40 animate-pulse rounded-md bg-muted"
        data-testid="runner-rules-loading"
      />
    )
  }

  const unruled = availableSources(toRows(rules))

  return (
    <div className="flex flex-col gap-1" data-testid={`runner-rules-${agentSlug}`}>
      <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
        Except when the work comes from
      </span>

      {rules.length === 0 && (
        <span className="text-[11px] text-muted-foreground">
          No exceptions — every source follows the default order.
        </span>
      )}

      {rules.map((r) => (
        <div key={r.source} data-testid={`runner-rule-${r.source}`}>
          <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-card px-2 py-1">
            <span className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 font-mono text-[11px] text-primary">
              {SOURCE_LABEL[r.source] ?? r.source}
            </span>
            <span className="text-muted-foreground">→</span>

            <select
              value={r.runner_id}
              onChange={(e) => mutate((rows) => nextRulesForRunner(rows, r.source, e.target.value))}
              aria-label={`Runner for ${r.source}`}
              className="rounded border border-input bg-input px-1.5 py-0.5 text-[12px] text-foreground"
            >
              {fleet.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.name}
                </option>
              ))}
            </select>

            {/* The strict toggle, worded as its consequence rather than as the
                flag name: "only" parks the queue when that box is down, which is
                the point for a source that can only run in one place. */}
            <div className="flex overflow-hidden rounded border border-input text-[10px]">
              <button
                type="button"
                onClick={() => {
                  if (!r.strict) mutate((rows) => nextRulesForStrict(rows, r.source))
                }}
                className={`px-2 py-0.5 ${
                  r.strict ? 'bg-primary text-primary-foreground' : 'text-muted-foreground'
                }`}
                aria-pressed={r.strict}
              >
                only
              </button>
              <button
                type="button"
                onClick={() => {
                  if (r.strict) mutate((rows) => nextRulesForStrict(rows, r.source))
                }}
                className={`px-2 py-0.5 ${
                  r.strict ? 'text-muted-foreground' : 'bg-primary text-primary-foreground'
                }`}
                aria-pressed={!r.strict}
              >
                fall through
              </button>
            </div>

            {/* Rules ARE deletable, unlike default-order chips (which toggle):
                a rule is cheap to re-add, and a greyed rule sitting next to a
                greyed runner would read as two different kinds of "off". */}
            <button
              type="button"
              onClick={() => mutate((rows) => nextRulesForRemove(rows, r.source))}
              aria-label={`Remove the ${r.source} rule`}
              className="ml-auto px-1 text-muted-foreground hover:text-destructive"
            >
              ✕
            </button>
          </div>

          {/* Strictness parking a queue is the toggle working; parking it
              SILENTLY is the failure. Say it, with the count. */}
          {r.strict && !r.online && (
            <p
              className="mt-0.5 pl-2 text-[11px] text-warning"
              data-testid={`runner-rule-parked-${r.source}`}
            >
              ⚠ {r.runner_name} is offline
              {r.queued_count > 0
                ? ` — ${r.queued_count} ${r.source} turn${r.queued_count === 1 ? '' : 's'} parked, and will stay parked.`
                : ' — this source will not run until it returns.'}
            </p>
          )}
        </div>
      ))}

      <div className="relative">
        <button
          type="button"
          onClick={() => setMenuOpen((v) => !v)}
          disabled={unruled.length === 0 || fleet.length === 0}
          className="w-fit rounded-md border border-input bg-input px-2 py-0.5 text-[11px] text-foreground-secondary hover:border-primary hover:text-primary disabled:opacity-40"
          data-testid="runner-rules-add-toggle"
        >
          + rule
        </button>
        {menuOpen && (
          <div className="absolute left-0 top-full z-10 mt-1 flex min-w-[11rem] flex-col gap-0.5 rounded-md border border-border bg-card p-1 shadow-md">
            {unruled.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => {
                  setMenuOpen(false)
                  mutate((rows) => nextRulesForAdd(rows, s, fleet[0].id))
                }}
                className="rounded px-2 py-1 text-left font-mono text-[11px] text-foreground hover:bg-muted"
              >
                {SOURCE_LABEL[s] ?? s}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* The precedence ladder, stated once where rules are edited — and the one
          place `enabled` means two things: a runner switched OFF in Default order
          still takes the work of any source that names it, because the rule is
          its own row with its own toggle. Left unsaid, a greyed chip above reads
          as "this box is off" while it is quietly answering email. */}
      <p className="text-[10px] text-foreground-subtle">
        A named runner wins over a rule; a live chat stays on the box hosting it.
        A rule still routes to its runner even when that runner is switched off above.
      </p>

      {error && <span className="text-[11px] text-destructive">{error}</span>}
    </div>
  )
}
