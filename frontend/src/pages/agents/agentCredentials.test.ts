import { describe, expect, it } from 'vitest'
import { blockers, groupRefs, type CredRow } from './agentCredentials'

// The question this screen exists to answer is "what is stopping this agent from
// running". On 2026-09-05 that question cost an SSH to a box and a `gog auth
// list`: ACE's mailbox had been dead since May and nothing surfaced it.

const row = (over: Partial<CredRow>): CredRow => ({
  name: 'x',
  declared: true,
  set: false,
  source: 'unset',
  updated_at: null,
  updated_by_email: null,
  ...over,
})

describe('blockers', () => {
  it('names the declared refs that are not set', () => {
    const b = blockers([
      row({ name: 'canopy-pat', set: true, source: 'canopy-web' }),
      row({ name: 'nova-api-key' }),
      row({ name: 'gog-token' }),
    ])
    expect(b.missing).toEqual(['nova-api-key', 'gog-token'])
  })

  it('ignores an UNDECLARED ref — an orphan is not a blocker', () => {
    // It still needs showing (a live secret nothing accounts for), but it can
    // never be the reason a turn won't run: nothing asks for it.
    const b = blockers([row({ name: 'retired', declared: false, set: true, source: 'canopy-web' })])
    expect(b.missing).toEqual([])
    expect(b.orphans).toEqual(['retired'])
  })

  it('is ready only when every declared ref is set', () => {
    expect(blockers([row({ set: true, source: 'canopy-web' })]).ready).toBe(true)
    expect(blockers([row({ set: false })]).ready).toBe(false)
  })

  it('an agent that declares nothing is not "ready" — it is undeclared', () => {
    // Zero refs and zero blockers is not the same as provisioned. Reporting
    // "ready" would say the box can run it, which nobody has established.
    const b = blockers([])
    expect(b.ready).toBe(false)
    expect(b.undeclared).toBe(true)
  })

  it('counts values still coming from 1Password as a migration residual', () => {
    // Not a blocker — resolution falls back — but it IS the thing that stops
    // "no vault access needed" from being true yet.
    const b = blockers([
      row({ name: 'a', set: true, source: 'canopy-web' }),
      row({ name: 'b', set: true, source: '1password' }),
    ])
    expect(b.ready).toBe(true)
    expect(b.stillInVault).toEqual(['b'])
  })
})

describe('groupRefs', () => {
  it('sorts unset-and-required to the top', () => {
    const g = groupRefs([
      row({ name: 'set-one', set: true, source: 'canopy-web' }),
      row({ name: 'missing-one' }),
    ])
    expect(g.map((r) => r.name)).toEqual(['missing-one', 'set-one'])
  })

  it('puts orphans last — they are noise relative to a blocker', () => {
    const g = groupRefs([
      row({ name: 'orphan', declared: false, set: true, source: 'canopy-web' }),
      row({ name: 'set-one', set: true, source: 'canopy-web' }),
      row({ name: 'missing-one' }),
    ])
    expect(g.map((r) => r.name)).toEqual(['missing-one', 'set-one', 'orphan'])
  })

  it('does not mutate the input', () => {
    const rows = [row({ name: 'b', set: true, source: 'canopy-web' }), row({ name: 'a' })]
    const before = JSON.stringify(rows)
    groupRefs(rows)
    expect(JSON.stringify(rows)).toBe(before)
  })
})
