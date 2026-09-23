import { type AgentRunnerRuleOut, type RoutableSource, type RuleTurnMode } from '@/api/agents'

export type { AgentRunnerRuleOut, RuleTurnMode }

// THE RULE MODEL behind the routing table (AgentRouting.tsx): the per-(source,
// actor) rows that sit above an agent's DEFAULT runner order. Pure transforms
// only — the table owns state and rendering.
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
// A rule also carries a MODE (spec 2026-09-23): '' says nothing and the row below
// decides; 'manual' / 'auto' override the agent's own turn mode for this work.
// An 'auto' naming a person applies only to a VERIFIED message from them — the
// server enforces that (apps/harness/turn_mode.py); the table says it.

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

export const SOURCE_LABEL: Record<string, string> = {
  ace_web: 'ace-web',
  email: 'email',
  canopy_scheduler: 'scheduler',
  canopy_web_chat: 'canopy chat',
  slack: 'slack',
  api: 'api (unclassified)',
}

// Sources whose turns have no human sender: a schedule fires on a clock, so a
// rule for one can only ever be an anyone-rule (apps/harness/actors.py).
export const ACTORLESS_SOURCES: ReadonlySet<string> = new Set(['canopy_scheduler'])

export type RuleRow = {
  source: string
  actor: string
  runnerIds: string[]
  strict: boolean
  turnMode: RuleTurnMode
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
  turnMode: RuleTurnMode
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
      turnMode: asRuleMode(first.turn_mode),
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
    turnMode: g.turnMode,
  }))
}

// The response carries a plain string (output schemas serialize what the DB
// holds); anything unrecognised reads as "says nothing", which is what the server
// does with it too.
export function asRuleMode(v: string | null | undefined): RuleTurnMode {
  return v === 'manual' || v === 'auto' ? v : ''
}

// What a rule whose mode is '' actually runs in: the next matching row down. A
// named-sender rule defers to its source's anyone-rule, and that to the agent.
export function inheritedMode(
  rule: { source: string; actor: string },
  rules: readonly { source: string; actor: string; turnMode: RuleTurnMode }[],
  agentMode: 'manual' | 'auto',
): 'manual' | 'auto' {
  if (rule.actor) {
    const anyone = rules.find((r) => r.source === rule.source && r.actor === '')
    if (anyone?.turnMode) return anyone.turnMode
  }
  return agentMode
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
  rules: readonly RuleRow[],
  source: string,
  runnerId: string,
  { actor = '', turnMode = '' }: { actor?: string; turnMode?: RuleTurnMode } = {},
): RuleRow[] {
  // strict=false by default: a new rule prefers a box without parking the queue
  // when that box is down. Opting into "wait" should be a deliberate click.
  return [...rules, { source, actor, runnerIds: [runnerId], strict: false, turnMode }]
}

export function nextRulesForMode(
  rules: readonly RuleRow[], key: string, turnMode: RuleTurnMode,
): RuleRow[] {
  return mapRule(rules, key, (r) => ({ ...r, turnMode }))
}

// Whether (source, actor) is already a rule — the add form's duplicate check.
// The actor is compared loosely (trimmed, lowercased) because the server stores
// the normalized address; it will still 422 a case this misses.
export function hasRule(
  rules: readonly { source: string; actor: string }[], source: string, actor: string,
): boolean {
  const a = actor.trim().toLowerCase()
  return rules.some((r) => r.source === source && r.actor.toLowerCase() === a)
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
