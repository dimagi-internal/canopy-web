import { describe, expect, it } from 'vitest'
import { groupRefs, summarize, type CredRow } from './agentCredentials'

// canopy-web deliberately does NOT hold most of an agent's secrets — 1Password
// does, reached on the box with the runner's service-account token. So this
// screen reports what canopy-web holds; it does not judge whether the agent can
// run, which is a question only the box can answer.

const row = (over: Partial<CredRow>): CredRow => ({
  name: 'x',
  declared: true,
  set: false,
  source: 'unset',
  updated_at: null,
  updated_by_email: null,
  ...over,
})

describe('summarize', () => {
  it('splits declared refs into stored-here and expected-from-the-vault', () => {
    const s = summarize([
      row({ name: 'canopy-pat', set: true, source: 'canopy-web' }),
      row({ name: 'ace-hq-api-key' }),
      row({ name: 'nova-api-key' }),
    ])
    expect(s.storedHere).toEqual(['canopy-pat'])
    expect(s.fromVault).toEqual(['ace-hq-api-key', 'nova-api-key'])
    expect(s.declaredCount).toBe(3)
  })

  it('does not treat vault-resolved refs as a deficit', () => {
    // The state every agent is in and mostly stays in. Reporting it as missing
    // made the page cry wolf on a healthy ACE — 45 "blockers" on an agent that
    // runs fine — which is how the one real dead credential gets ignored.
    const s = summarize([row({ name: 'a' }), row({ name: 'b' })])
    expect(s.fromVault).toEqual(['a', 'b'])
    expect('selfContained' in s).toBe(false)
    expect('missing' in s).toBe(false)
  })

  it('surfaces orphans and excludes them from the declared count', () => {
    const s = summarize([
      row({ name: 'declared', set: true, source: 'canopy-web' }),
      row({ name: 'retired', declared: false, set: true, source: 'canopy-web' }),
    ])
    expect(s.orphans).toEqual(['retired'])
    expect(s.declaredCount).toBe(1)
  })

  it('an agent declaring nothing is undeclared, not provisioned', () => {
    const s = summarize([])
    expect(s.undeclared).toBe(true)
    expect(s.declaredCount).toBe(0)
  })
})

describe('groupRefs', () => {
  it('puts what this page manages first, then vault-resolved, then orphans', () => {
    const g = groupRefs([
      row({ name: 'orphan', declared: false, set: true, source: 'canopy-web' }),
      row({ name: 'vault' }),
      row({ name: 'here', set: true, source: 'canopy-web' }),
    ])
    expect(g.map((r) => r.name)).toEqual(['here', 'vault', 'orphan'])
  })

  it('does not mutate the input', () => {
    const rows = [row({ name: 'b', set: true, source: 'canopy-web' }), row({ name: 'a' })]
    const before = JSON.stringify(rows)
    groupRefs(rows)
    expect(JSON.stringify(rows)).toBe(before)
  })
})
