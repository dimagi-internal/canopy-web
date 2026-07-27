import { describe, it, expect } from 'vitest'
import { pairNarrationScenes, hasNarrativeChanges } from './narrativeScenePairing'
import type { DddNarration } from '../../api/ddd'

const scene = (id: string, text: string, extra: Partial<DddNarration> = {}): DddNarration => ({
  id,
  text,
  ...extra,
})

describe('pairNarrationScenes', () => {
  it('marks unchanged scenes (ignoring whitespace)', () => {
    const before = [scene('n1', 'The  study begins.')]
    const after = [scene('n1', 'The study begins.')]
    const pairs = pairNarrationScenes(before, after)
    expect(pairs).toHaveLength(1)
    expect(pairs[0].status).toBe('unchanged')
    expect(hasNarrativeChanges(pairs)).toBe(false)
  })

  it('marks changed scenes and keeps both texts', () => {
    const before = [scene('n1', 'measure the real effect')]
    const after = [scene('n1', 'measure differences in outcomes')]
    const pairs = pairNarrationScenes(before, after)
    expect(pairs[0].status).toBe('changed')
    expect(pairs[0].before).toBe('measure the real effect')
    expect(pairs[0].after).toBe('measure differences in outcomes')
    expect(hasNarrativeChanges(pairs)).toBe(true)
  })

  it('matches by id even when scenes are reordered', () => {
    const before = [scene('a', 'A'), scene('b', 'B')]
    const after = [scene('b', 'B'), scene('a', 'A2')]
    const pairs = pairNarrationScenes(before, after)
    // Output follows the after order: b first, then a.
    expect(pairs.map((p) => p.id)).toEqual(['b', 'a'])
    expect(pairs[0].status).toBe('unchanged')
    expect(pairs[1].status).toBe('changed')
  })

  it('flags added scenes (present only in after)', () => {
    const before = [scene('n1', 'one')]
    const after = [scene('n1', 'one'), scene('n2', 'brand new beat')]
    const pairs = pairNarrationScenes(before, after)
    expect(pairs[1].status).toBe('added')
    expect(pairs[1].before).toBeNull()
    expect(pairs[1].after).toBe('brand new beat')
  })

  it('flags removed scenes (present only in before) and appends them', () => {
    const before = [scene('n1', 'one'), scene('gone', 'dropped beat')]
    const after = [scene('n1', 'one')]
    const pairs = pairNarrationScenes(before, after)
    expect(pairs).toHaveLength(2)
    expect(pairs[1].status).toBe('removed')
    expect(pairs[1].before).toBe('dropped beat')
    expect(pairs[1].after).toBeNull()
  })

  it('falls back to positional pairing for id-less scenes', () => {
    const before = [{ text: 'first' }, { text: 'second' }]
    const after = [{ text: 'first' }, { text: 'second edited' }]
    const pairs = pairNarrationScenes(before, after)
    expect(pairs[0].status).toBe('unchanged')
    expect(pairs[1].status).toBe('changed')
  })
})

describe('legacy histories whose ids were derived from titles', () => {
  // Real ids from verified-monitoring on labs, 2026-07-26. The author reworded
  // every scene title between v16 and v17, and because pre-L0 ids were derived
  // from the title, ZERO of six ids overlap — so id-matching reported six
  // "New" scenes and no before/after, on the one narrative a domain expert had
  // just iterated. This is the case the positional fallback exists for.
  const v16 = [
    { id: 'the-survey-verifies-the-program-now-maya-verifies-the-survey', text: 'Old beat one.' },
    { id: 'the-gap-is-real-where-delivery-happened-vs-where-the-survey-checked', text: 'Old beat two.' },
    { id: 'the-per-surveyor-scorecard-flags-a-surveyor-and-holds-the-work', text: 'Old beat three.' },
    { id: 'the-independent-back-check-the-answers-don-t-match', text: 'Old beat four.' },
    { id: 'the-distribution-signals-warning-signs-held-for-investigation', text: 'Old beat five.' },
  ]
  const v17 = [
    { id: 'the-goal-independent-drillable-proof-the-program-works', text: 'Old beat one.' },
    { id: 'six-bi-monthly-rounds-over-time-the-gap-holds', text: 'NEW beat two.' },
    { id: 'where-delivery-happened-vs-where-the-survey-checked-the-map-moves', text: 'Old beat three.' },
    { id: 'the-per-surveyor-quality-scorecard-catches-a-surveyor', text: 'NEW beat four.' },
    { id: 'the-independent-back-check-confirms-it', text: 'Old beat five.' },
    { id: 'gps-is-clean-the-distributions-screen-catches-the-fabrication', text: 'A genuinely new sixth beat.' },
  ]

  it('pairs positionally when no id is shared, instead of reporting all-new', () => {
    const pairs = pairNarrationScenes(v16, v17)
    expect(pairs.map((p) => p.status)).toEqual([
      'unchanged', 'changed', 'unchanged', 'changed', 'unchanged', 'added',
    ])
    expect(pairs[1].before).toBe('Old beat two.')
    expect(pairs[1].after).toBe('NEW beat two.')
  })

  it('still matches on id when the ids ARE comparable (post-L0 work)', () => {
    const before = [{ id: 'the-goal', text: 'Was.' }, { id: 'the-proof', text: 'Same.' }]
    const after = [{ id: 'the-proof', text: 'Same.' }, { id: 'the-goal', text: 'Now.' }]
    const pairs = pairNarrationScenes(before, after)
    // Reorder-safe: matched by id, not position.
    expect(pairs.find((p) => p.id === 'the-goal')?.status).toBe('changed')
    expect(pairs.find((p) => p.id === 'the-proof')?.status).toBe('unchanged')
  })

  it('does not fall back when one side is empty (a first version stays all-new)', () => {
    const pairs = pairNarrationScenes([], [{ id: 'a', text: 'First ever.' }])
    expect(pairs.map((p) => p.status)).toEqual(['added'])
  })
})
