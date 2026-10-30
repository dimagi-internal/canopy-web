import { describe, expect, it } from 'vitest'
import { sections, type CredRow } from './agentCredentials'

// This screen reports what the agent's Canopy vault holds; it does not judge
// whether the agent can run, which is a question only the box can answer.

const row = (over: Partial<CredRow>): CredRow => ({
  name: 'x',
  declared: true,
  set: false,
  source: 'unset',
  updated_at: null,
  updated_by_email: null,
  read_count: 0,
  account: 'agent',
  account_label: '',
  ...over,
})

describe('sections', () => {
  const unset = (n: string) => row({ name: n })
  const here = (n: string) => row({ name: n, set: true, source: 'canopy-web' })

  it('separates what is stored from what is declared but has no value', () => {
    const s = sections([unset('v1'), here('h1'), unset('v2')])
    expect(s.notSet.map((r) => r.name)).toEqual(['v1', 'v2'])
    expect(s.storedHere.map((r) => r.name)).toEqual(['h1'])
    expect(s.orphans).toEqual([])
  })

  it('an orphan is a stored secret nothing declares', () => {
    const s = sections([unset('a'), row({ name: 'retired', declared: false, set: true })])
    expect(s.orphans.map((r) => r.name)).toEqual(['retired'])
  })

  it('does not mutate the input', () => {
    const rows = [unset('b'), unset('a')]
    const before = JSON.stringify(rows)
    sections(rows)
    expect(JSON.stringify(rows)).toBe(before)
  })
})

describe('sections — a mailbox implies gog-token', () => {
  it('does not call a minted gog-token an orphan for an agent with a mailbox', () => {
    // ada after "Connect Google mailbox": stored, never declared.
    const rows = [{ name: 'gog-token', declared: false, set: true, source: 'canopy-web',
                    updated_at: null, updated_by_email: null }] as never
    expect(sections(rows).orphans).toHaveLength(1)
    const s = sections(rows, ['gog-token'])
    expect(s.orphans).toHaveLength(0)
    expect(s.storedHere.map((r) => r.name)).toEqual(['gog-token'])
  })
})
