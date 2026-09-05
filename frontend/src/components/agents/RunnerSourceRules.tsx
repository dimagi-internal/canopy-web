import { useEffect, useRef, useState, type JSX } from 'react'
import {
  getAgentRunnerRules,
  putAgentRunnerRules,
  type AgentRunnerRuleOut,
  type RoutableSource,
} from '@/api/agents'
import { type RunnerOut } from '@/api/harness'

export type { AgentRunnerRuleOut }

// The per-(source, actor) exception list that sits under an agent's DEFAULT
// runner order (see RunnerAssignments).
//
// A rule is (source, actor) -> an ORDERED LIST of runners, plus a strict toggle.
// It names a LIST rather than one runner because the operator's own boxes are two
// macOS accounts on one machine, alternated as each runs out of tokens: "my work,
// on either of mine, never cloud" cannot be said with a single runner — naming the
// logged-out account parks the queue roughly half the time, and falling through
// hands the work to whatever sits next in the default order (on a cloud-default
// agent, cloud). Spec: 2026-09-05-actor-aware-runner-routing-design.md.
//
// `actor: ''` means "any actor" — bit-for-bit the pre-actor source rule — so an
// existing rule is simply a rule of length one with an empty actor, and renders
// and edits exactly as it did.
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

export type RuleRow = {
  source: string
  actor: string
  runnerIds: string[]
  strict: boolean
}

// A rule's identity is the PAIR. Keying on source alone is what the pre-actor
// version did, and it is why several actors could not share a source.
// NUL-joined so an actor containing the separator can't forge another rule's key.
export function ruleKey(r: { source: string; actor: string }): string {
  return `${r.source}\u0000${r.actor}`
}

export type GroupedRule = {
  source: string
  actor: string
  strict: boolean
  runners: AgentRunnerRuleOut[]
  queuedCount: number
  // A STRICT rule with no online runner left. Not "its first runner is offline":
  // naming two boxes exists precisely so one being asleep is not a parked queue.
  // A fall-through rule is never parked — it degrades to the default order.
  parked: boolean
}

// The API returns rows FLAT, one per runner, so the response shape stays the one
// the frontend already consumes (name/kind/online/ready are per runner anyway).
// Grouping happens here.
export function groupRules(rules: readonly AgentRunnerRuleOut[]): GroupedRule[] {
  const byKey = new Map<string, AgentRunnerRuleOut[]>()
  for (const row of rules) {
    const k = ruleKey(row)
    const bucket = byKey.get(k)
    if (bucket) bucket.push(row)
    else byKey.set(k, [row])
  }
  const out: GroupedRule[] = []
  for (const rows of byKey.values()) {
    const runners = [...rows].sort((a, b) => (a.rank ?? 0) - (b.rank ?? 0))
    const first = runners[0]
    out.push({
      source: first.source,
      actor: first.actor ?? '',
      strict: first.strict,
      runners,
      queuedCount: first.queued_count ?? 0,
      parked: first.strict && !runners.some((r) => r.online),
    })
  }
  // Specific rules above the catch-all, matching the order the cascade evaluates
  // them in, so reading the list top-down reads the precedence.
  return out.sort(
    (a, b) =>
      a.source.localeCompare(b.source) ||
      (a.actor === '' ? 1 : 0) - (b.actor === '' ? 1 : 0) ||
      a.actor.localeCompare(b.actor),
  )
}

export function toRows(rules: readonly AgentRunnerRuleOut[]): RuleRow[] {
  return groupRules(rules).map((g) => ({
    source: g.source,
    actor: g.actor,
    runnerIds: g.runners.map((r) => r.runner_id),
    strict: g.strict,
  }))
}

// Pure list transforms, extracted so they unit-test without a React renderer
// (see RunnerSourceRules.test.tsx). None of them mutates its input — the commit
// lane keeps a `prev` snapshot to revert to on failure.
function mapRule(
  rules: readonly RuleRow[], key: string, fn: (r: RuleRow) => RuleRow,
): RuleRow[] {
  return rules.map((r) => (ruleKey(r) === key ? fn(r) : r))
}

export function nextRulesForAdd(
  rules: readonly RuleRow[], source: string, runnerId: string,
): RuleRow[] {
  // strict=false by default: a new rule prefers a box without parking the queue
  // when that box is down. Opting into "only" should be a deliberate click.
  // actor='' by default: a rule is born as a plain source rule, and naming a
  // person is the second, explicit step.
  return [...rules, { source, actor: '', runnerIds: [runnerId], strict: false }]
}

export function nextRulesForActor(
  rules: readonly RuleRow[], key: string, actor: string,
): RuleRow[] {
  return mapRule(rules, key, (r) => ({ ...r, actor }))
}

export function nextRulesForStrict(rules: readonly RuleRow[], key: string): RuleRow[] {
  return mapRule(rules, key, (r) => ({ ...r, strict: !r.strict }))
}

export function nextRulesForRemove(rules: readonly RuleRow[], key: string): RuleRow[] {
  return rules.filter((r) => ruleKey(r) !== key)
}

export function nextRulesForRunnerAdd(
  rules: readonly RuleRow[], key: string, runnerId: string,
): RuleRow[] {
  // A runner twice in one rule is a 422 server-side; refusing here keeps the
  // optimistic render honest rather than showing a row the save will reject.
  return mapRule(rules, key, (r) =>
    r.runnerIds.includes(runnerId) ? r : { ...r, runnerIds: [...r.runnerIds, runnerId] },
  )
}

export function nextRulesForRunnerMove(
  rules: readonly RuleRow[], key: string, index: number, delta: number,
): RuleRow[] {
  return mapRule(rules, key, (r) => {
    const to = index + delta
    if (to < 0 || to >= r.runnerIds.length) return r
    const ids = [...r.runnerIds]
    ;[ids[index], ids[to]] = [ids[to], ids[index]]
    return { ...r, runnerIds: ids }
  })
}

export function nextRulesForRunnerRemove(
  rules: readonly RuleRow[], key: string, runnerId: string,
): RuleRow[] {
  // Dropping the last runner deletes the RULE. A zero-runner rule is a 422: a
  // strict one would compose to an empty list and park the queue naming no
  // runner as the reason.
  return rules.flatMap((r) => {
    if (ruleKey(r) !== key) return [r]
    const ids = r.runnerIds.filter((id) => id !== runnerId)
    return ids.length ? [{ ...r, runnerIds: ids }] : []
  })
}

export function availableSources(rules: readonly RuleRow[]): RoutableSource[] {
  // Only a CATCH-ALL rule (actor: '') consumes a source — several actors may
  // share one, which is the feature. A source whose only rules name people must
  // still be addable for everyone else.
  const taken = new Set(rules.filter((r) => r.actor === '').map((r) => r.source))
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
    // immediately rather than flashing empty until the PUT returns. Flattened
    // back to one row per runner, which is the shape the server returns.
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
          } as AgentRunnerRuleOut
        }),
      ),
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

  const grouped = groupRules(rules)
  const unruled = availableSources(toRows(rules))
  const unused = (g: GroupedRule) =>
    fleet.filter((f) => !g.runners.some((r) => r.runner_id === f.id))

  return (
    <div className="flex flex-col gap-1" data-testid={`runner-rules-${agentSlug}`}>
      <span className="text-[10px] uppercase tracking-wide text-muted-foreground">
        Except when the work comes from
      </span>

      {grouped.length === 0 && (
        <span className="text-[11px] text-muted-foreground">
          No exceptions — every source follows the default order.
        </span>
      )}

      {grouped.map((g) => {
        const key = ruleKey(g)
        const testId = g.actor ? `${g.source}-${g.actor}` : g.source
        return (
          <div key={key} data-testid={`runner-rule-${testId}`}>
            <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-card px-2 py-1">
              <span className="rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 font-mono text-[11px] text-primary">
                {SOURCE_LABEL[g.source] ?? g.source}
              </span>

              {/* Free text, placeholder `anyone`: an operator pastes what their
                  mail client shows, and the server normalizes a full
                  "Name <addr>" header down to the bare address it routes on. */}
              <span className="text-[11px] text-muted-foreground">from</span>
              <input
                type="text"
                defaultValue={g.actor}
                placeholder="anyone"
                onBlur={(e) => {
                  const v = e.target.value.trim()
                  if (v !== g.actor) mutate((rows) => nextRulesForActor(rows, key, v))
                }}
                aria-label={`Actor for the ${g.source} rule`}
                className="w-52 rounded border border-input bg-input px-1.5 py-0.5 font-mono text-[11px] text-foreground"
              />
              <span className="text-muted-foreground">→</span>

              {/* The rule's runners as the SAME rank chip row the default order
                  uses — order is the preference, and a rule may name several
                  because the operator's two macOS accounts alternate. */}
              <div className="flex flex-wrap items-center gap-1">
                {g.runners.map((r, i) => (
                  <span
                    key={r.runner_id}
                    className="flex items-center gap-1 rounded border border-input bg-input px-1.5 py-0.5 text-[11px]"
                    data-testid={`rule-runner-${testId}-${r.runner_name}`}
                  >
                    <span className={r.online ? 'text-success' : 'text-muted-foreground'}>●</span>
                    <span className="font-mono">{r.runner_name}</span>
                    <button
                      type="button"
                      onClick={() => mutate((rows) => nextRulesForRunnerMove(rows, key, i, -1))}
                      disabled={i === 0}
                      aria-label={`Move ${r.runner_name} up in the ${g.source} rule`}
                      className="text-muted-foreground hover:text-primary disabled:opacity-30"
                    >
                      ↑
                    </button>
                    <button
                      type="button"
                      onClick={() => mutate((rows) => nextRulesForRunnerMove(rows, key, i, 1))}
                      disabled={i === g.runners.length - 1}
                      aria-label={`Move ${r.runner_name} down in the ${g.source} rule`}
                      className="text-muted-foreground hover:text-primary disabled:opacity-30"
                    >
                      ↓
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        mutate((rows) => nextRulesForRunnerRemove(rows, key, r.runner_id))
                      }
                      aria-label={`Remove ${r.runner_name} from the ${g.source} rule`}
                      className="text-muted-foreground hover:text-destructive"
                    >
                      ✕
                    </button>
                  </span>
                ))}
                {unused(g).length > 0 && (
                  <select
                    value=""
                    onChange={(e) => {
                      if (e.target.value) {
                        mutate((rows) => nextRulesForRunnerAdd(rows, key, e.target.value))
                      }
                    }}
                    aria-label={`Add a runner to the ${g.source} rule`}
                    className="rounded border border-input bg-input px-1 py-0.5 text-[11px] text-foreground-secondary"
                  >
                    <option value="">+</option>
                    {unused(g).map((f) => (
                      <option key={f.id} value={f.id}>
                        {f.name}
                      </option>
                    ))}
                  </select>
                )}
              </div>

              {/* The strict toggle, worded as its consequence rather than as the
                  flag name: "only" parks the queue when every runner it names is
                  down, which is the point for work that can only run in one place. */}
              <div className="flex overflow-hidden rounded border border-input text-[10px]">
                <button
                  type="button"
                  onClick={() => {
                    if (!g.strict) mutate((rows) => nextRulesForStrict(rows, key))
                  }}
                  className={`px-2 py-0.5 ${
                    g.strict ? 'bg-primary text-primary-foreground' : 'text-muted-foreground'
                  }`}
                  aria-pressed={g.strict}
                >
                  only
                </button>
                <button
                  type="button"
                  onClick={() => {
                    if (g.strict) mutate((rows) => nextRulesForStrict(rows, key))
                  }}
                  className={`px-2 py-0.5 ${
                    g.strict ? 'text-muted-foreground' : 'bg-primary text-primary-foreground'
                  }`}
                  aria-pressed={!g.strict}
                >
                  fall through
                </button>
              </div>

              {/* Rules ARE deletable, unlike default-order chips (which toggle):
                  a rule is cheap to re-add, and a greyed rule sitting next to a
                  greyed runner would read as two different kinds of "off". */}
              <button
                type="button"
                onClick={() => mutate((rows) => nextRulesForRemove(rows, key))}
                aria-label={`Remove the ${g.source} rule`}
                className="ml-auto px-1 text-muted-foreground hover:text-destructive"
              >
                ✕
              </button>
            </div>

            {/* Strictness parking a queue is the toggle working; parking it
                SILENTLY is the failure. Say it, with the count — and only when
                EVERY runner the rule names is down, since naming two boxes
                exists precisely so one being asleep is not a parked queue. */}
            {g.parked && (
              <p
                className="mt-0.5 pl-2 text-[11px] text-warning"
                data-testid={`runner-rule-parked-${testId}`}
              >
                ⚠ {g.runners.map((r) => r.runner_name).join(' and ')}{' '}
                {g.runners.length > 1 ? 'are' : 'is'} offline
                {g.queuedCount > 0
                  ? ` — ${g.queuedCount} ${g.source}${g.actor ? ` turn${g.queuedCount === 1 ? '' : 's'} from ${g.actor}` : ` turn${g.queuedCount === 1 ? '' : 's'}`} parked, and will stay parked.`
                  : ' — this work will not run until one returns.'}
              </p>
            )}
          </div>
        )
      })}

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
          still takes the work of any rule that names it, because the rule is its
          own row with its own toggle. Left unsaid, a greyed chip above reads as
          "this box is off" while it is quietly answering email. */}
      <p className="text-[10px] text-foreground-subtle">
        A named runner wins over a rule; a live chat stays on the box hosting it; a rule naming
        a person beats one naming only a source, which beats the default order. A rule still
        routes to its runners even when they are switched off above.
      </p>

      {error && <span className="text-[11px] text-destructive">{error}</span>}
    </div>
  )
}
