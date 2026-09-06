import { describe, expect, it } from 'vitest'
import { headline, sections, type CredRow } from './agentCredentials'

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

describe('sections + headline', () => {
  const vault = (n: string) => row({ name: n })
  const here = (n: string) => row({ name: n, set: true, source: 'canopy-web' })

  it('says nothing is wanted when every secret lives in the vault', () => {
    // The state ACE is in, and the state most agents will stay in. The old copy
    // ("canopy-web stores 0 of 45 declared secrets") is a fact about the store;
    // the reader wants a fact about whether they have work to do.
    const rows = ['a', 'b', 'c'].map(vault)
    expect(headline(rows)).toMatch(/Nothing here needs you/)
    expect(headline(rows)).toContain('3')
  })

  it('separates the vault-resolved refs from what this page manages', () => {
    const s = sections([vault('v1'), here('h1'), vault('v2')])
    expect(s.fromVault.map((r) => r.name)).toEqual(['v1', 'v2'])
    expect(s.storedHere.map((r) => r.name)).toEqual(['h1'])
    expect(s.orphans).toEqual([])
  })

  it('an orphan outranks everything else in the headline', () => {
    // A live secret nothing declares is the one thing on this screen that is
    // actually wrong, so it must not be averaged in with 45 healthy rows.
    const rows = [vault('a'), vault('b'), row({ name: 'retired', declared: false, set: true })]
    expect(headline(rows)).toMatch(/nothing declares/)
  })

  it('reports both sides once something is stored here', () => {
    expect(headline([here('h'), vault('v')])).toMatch(/1 stored here; 1 resolve/)
  })

  it('does not mutate the input', () => {
    const rows = [vault('b'), vault('a')]
    const before = JSON.stringify(rows)
    sections(rows)
    expect(JSON.stringify(rows)).toBe(before)
  })
})

it('never frames a vault-resolved ref as a deficit', () => {
  // The guard that used to sit on `summarize`, moved onto the live path when
  // that helper was deleted. An earlier version measured "every declared secret
  // is stored here" as the success state, which both rewarded copying all 45 of
  // ACE's secrets into canopy-web AND made the screen announce "45 blockers —
  // this agent cannot run" about an agent running perfectly well. A screen that
  // cries wolf is how the one genuinely dead credential gets ignored.
  const text = headline(['a', 'b', 'c'].map((name) => row({ name })))
  expect(text).not.toMatch(/missing|blocker|cannot run|incomplete|not ready/i)
})
