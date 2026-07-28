import { describe, expect, it } from 'vitest'
import {
  availableSources,
  nextRulesForAdd,
  nextRulesForRemove,
  nextRulesForRunner,
  nextRulesForStrict,
  type RuleRow,
} from './RunnerSourceRules'

const rules: RuleRow[] = [
  { source: 'ace_web', runnerId: 'r-cloud', strict: true },
  { source: 'email', runnerId: 'r-laptop', strict: false },
]

describe('rule list transforms', () => {
  it('adds a rule defaulting to fall-through', () => {
    // Fall-through is the safe default: a new rule prefers a box without
    // parking the queue when that box is down.
    const next = nextRulesForAdd(rules, 'canopy_scheduler', 'r-cloud')
    expect(next).toHaveLength(3)
    expect(next[2]).toEqual({ source: 'canopy_scheduler', runnerId: 'r-cloud', strict: false })
  })

  it('repoints one rule without touching the others', () => {
    const next = nextRulesForRunner(rules, 'email', 'r-cloud')
    expect(next[1].runnerId).toBe('r-cloud')
    expect(next[0]).toEqual(rules[0])
  })

  it('flips strict in place', () => {
    expect(nextRulesForStrict(rules, 'email')[1].strict).toBe(true)
    expect(nextRulesForStrict(rules, 'email')[0]).toEqual(rules[0])
  })

  it('removes by source', () => {
    expect(nextRulesForRemove(rules, 'ace_web').map((r) => r.source)).toEqual(['email'])
  })

  it('never mutates the input list', () => {
    // The commit lane keeps a `prev` snapshot to revert to on error; a mutating
    // transform would corrupt it.
    const before = JSON.stringify(rules)
    nextRulesForAdd(rules, 'slack', 'r-cloud')
    nextRulesForStrict(rules, 'email')
    nextRulesForRemove(rules, 'email')
    expect(JSON.stringify(rules)).toBe(before)
  })

  it('offers only sources not already ruled on', () => {
    expect(availableSources(rules)).not.toContain('ace_web')
    expect(availableSources(rules)).not.toContain('email')
    expect(availableSources(rules)).toContain('canopy_scheduler')
  })

  it('offers nothing once every source has a rule', () => {
    const all = availableSources([]).map((s) => ({ source: s, runnerId: 'r', strict: false }))
    expect(availableSources(all)).toEqual([])
  })
})
