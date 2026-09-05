import { describe, expect, it } from 'vitest'
import {
  availableSources,
  groupRules,
  nextRulesForActor,
  nextRulesForAdd,
  nextRulesForRemove,
  nextRulesForRunnerAdd,
  nextRulesForRunnerMove,
  nextRulesForRunnerRemove,
  nextRulesForStrict,
  ruleKey,
  type AgentRunnerRuleOut,
  type RuleRow,
} from './RunnerSourceRules'

// A rule is now (source, actor) -> an ORDERED list of runners. `actor: ''` means
// "any actor", which is exactly what a source rule meant before actors existed —
// so every pre-actor rule is just a rule of length one with an empty actor.
const rules: RuleRow[] = [
  { source: 'ace_web', actor: '', runnerIds: ['r-cloud'], strict: true },
  { source: 'email', actor: '', runnerIds: ['r-laptop'], strict: false },
]

const K = (source: string, actor = '') => ruleKey({ source, actor })

describe('rule list transforms', () => {
  it('adds a rule defaulting to fall-through and no actor', () => {
    // Fall-through is the safe default: a new rule prefers a box without
    // parking the queue when that box is down.
    const next = nextRulesForAdd(rules, 'canopy_scheduler', 'r-cloud')
    expect(next).toHaveLength(3)
    expect(next[2]).toEqual({
      source: 'canopy_scheduler', actor: '', runnerIds: ['r-cloud'], strict: false,
    })
  })

  it('flips strict in place', () => {
    expect(nextRulesForStrict(rules, K('email'))[1].strict).toBe(true)
    expect(nextRulesForStrict(rules, K('email'))[0]).toEqual(rules[0])
  })

  it('removes by (source, actor), not by source alone', () => {
    const two: RuleRow[] = [
      { source: 'email', actor: 'jj@dimagi.com', runnerIds: ['r-laptop'], strict: true },
      { source: 'email', actor: 'stewari@dimagi.com', runnerIds: ['r-cloud'], strict: true },
    ]
    const next = nextRulesForRemove(two, K('email', 'jj@dimagi.com'))
    expect(next.map((r) => r.actor)).toEqual(['stewari@dimagi.com'])
  })

  it('never mutates the input list', () => {
    // The commit lane keeps a `prev` snapshot to revert to on error; a mutating
    // transform would corrupt it.
    const before = JSON.stringify(rules)
    nextRulesForAdd(rules, 'slack', 'r-cloud')
    nextRulesForStrict(rules, K('email'))
    nextRulesForRemove(rules, K('email'))
    nextRulesForRunnerAdd(rules, K('email'), 'r-cloud')
    nextRulesForRunnerMove(rules, K('email'), 0, 1)
    expect(JSON.stringify(rules)).toBe(before)
  })
})

describe('actors', () => {
  it('sets the actor on one rule only', () => {
    const next = nextRulesForActor(rules, K('email'), 'stewari@dimagi.com')
    expect(next[1].actor).toBe('stewari@dimagi.com')
    expect(next[0]).toEqual(rules[0])
  })

  it('lets two actors share one source', () => {
    // The whole feature: Sarvesh's mail to cloud, mine to my own box.
    let next = nextRulesForAdd(rules, 'email', 'r-cloud')
    next = nextRulesForActor(next, K('email'), 'stewari@dimagi.com')
    expect(next.filter((r) => r.source === 'email')).toHaveLength(2)
  })

  it('offers a source again once its rules all name an actor', () => {
    // Only the catch-all (actor: '') consumes a source; a source with only
    // actor-specific rules still needs to be addable for everyone else.
    const specific: RuleRow[] = [
      { source: 'email', actor: 'jj@dimagi.com', runnerIds: ['r-laptop'], strict: true },
    ]
    expect(availableSources(specific)).toContain('email')
  })

  it('stops offering a source that already has a catch-all rule', () => {
    expect(availableSources(rules)).not.toContain('ace_web')
    expect(availableSources(rules)).not.toContain('email')
    expect(availableSources(rules)).toContain('canopy_scheduler')
  })

  it('offers nothing once every source has a catch-all rule', () => {
    const all = availableSources([]).map((s) => ({
      source: s, actor: '', runnerIds: ['r'], strict: false,
    }))
    expect(availableSources(all)).toEqual([])
  })
})

describe('a rule names several runners', () => {
  // Why this exists: jj-mbp-cdp and acedimagi-mbp-cdp are two macOS accounts on
  // ONE machine, alternated as each runs out of tokens. "My work, never cloud"
  // therefore names two runners whose live one rotates — unsayable with one.
  const boxes: RuleRow[] = [
    { source: 'email', actor: 'jj@dimagi.com', runnerIds: ['r-acedimagi'], strict: true },
  ]

  it('appends a runner to a rule', () => {
    const next = nextRulesForRunnerAdd(boxes, K('email', 'jj@dimagi.com'), 'r-jj')
    expect(next[0].runnerIds).toEqual(['r-acedimagi', 'r-jj'])
  })

  it('refuses to add the same runner twice — the server 422s on it', () => {
    const next = nextRulesForRunnerAdd(boxes, K('email', 'jj@dimagi.com'), 'r-acedimagi')
    expect(next[0].runnerIds).toEqual(['r-acedimagi'])
  })

  it('reorders runners, because order is the preference', () => {
    const two = nextRulesForRunnerAdd(boxes, K('email', 'jj@dimagi.com'), 'r-jj')
    const next = nextRulesForRunnerMove(two, K('email', 'jj@dimagi.com'), 1, -1)
    expect(next[0].runnerIds).toEqual(['r-jj', 'r-acedimagi'])
  })

  it('ignores a move off either end', () => {
    const next = nextRulesForRunnerMove(boxes, K('email', 'jj@dimagi.com'), 0, -1)
    expect(next[0].runnerIds).toEqual(['r-acedimagi'])
  })

  it('removes a runner from a rule', () => {
    const two = nextRulesForRunnerAdd(boxes, K('email', 'jj@dimagi.com'), 'r-jj')
    const next = nextRulesForRunnerRemove(two, K('email', 'jj@dimagi.com'), 'r-acedimagi')
    expect(next[0].runnerIds).toEqual(['r-jj'])
  })

  it('drops the whole rule when its last runner goes', () => {
    // A zero-runner rule is a 422 server-side: a strict one would compose to an
    // empty list and park the queue naming no runner as the reason.
    const next = nextRulesForRunnerRemove(boxes, K('email', 'jj@dimagi.com'), 'r-acedimagi')
    expect(next).toEqual([])
  })
})

describe('groupRules', () => {
  // The API returns rows FLAT — one per runner — so the component groups them.
  const flat = [
    { source: 'email', actor: 'jj@dimagi.com', rank: 1, runner_id: 'r-jj', runner_name: 'jj-mbp',
      kind: 'emdash', strict: true, online: false, ready: true, enabled: true, queued_count: 3 },
    { source: 'email', actor: 'jj@dimagi.com', rank: 0, runner_id: 'r-ace', runner_name: 'acedimagi',
      kind: 'emdash', strict: true, online: true, ready: true, enabled: true, queued_count: 3 },
    { source: 'ace_web', actor: '', rank: 0, runner_id: 'r-cloud', runner_name: 'cloud-1',
      kind: 'cloud', strict: false, online: true, ready: true, enabled: true, queued_count: 0 },
  ] as unknown as AgentRunnerRuleOut[]

  it('groups rows into rules and orders runners by rank', () => {
    const grouped = groupRules(flat)
    expect(grouped).toHaveLength(2)
    const mine = grouped.find((g) => g.actor === 'jj@dimagi.com')!
    expect(mine.runners.map((r) => r.runner_name)).toEqual(['acedimagi', 'jj-mbp'])
  })

  it('sorts specific rules above the catch-all, matching evaluation order', () => {
    const grouped = groupRules([
      { source: 'email', actor: '', rank: 0, runner_id: 'r-cloud' },
      { source: 'email', actor: 'jj@dimagi.com', rank: 0, runner_id: 'r-jj' },
    ] as unknown as AgentRunnerRuleOut[])
    expect(grouped.map((g) => g.actor)).toEqual(['jj@dimagi.com', ''])
  })

  it('is parked only when EVERY runner of the rule is offline', () => {
    // The point of naming two boxes: one being asleep is not a parked queue.
    const grouped = groupRules(flat)
    expect(grouped.find((g) => g.actor === 'jj@dimagi.com')!.parked).toBe(false)
  })

  it('is parked when a strict rule has no online runner left', () => {
    const allDown = flat
      .filter((r) => r.actor === 'jj@dimagi.com')
      .map((r) => ({ ...r, online: false })) as unknown as AgentRunnerRuleOut[]
    expect(groupRules(allDown)[0].parked).toBe(true)
  })

  it('a fall-through rule never reads as parked — it degrades instead', () => {
    const soft = flat
      .filter((r) => r.actor === 'jj@dimagi.com')
      .map((r) => ({ ...r, online: false, strict: false })) as unknown as AgentRunnerRuleOut[]
    expect(groupRules(soft)[0].parked).toBe(false)
  })
})
