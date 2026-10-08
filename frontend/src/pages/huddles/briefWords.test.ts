import { describe, expect, it } from 'vitest'
import {
  briefItems, briefNotNow, costWords, failsWords, leverWords, needWords, priorityNumber, reportItemWords, servesWords,
} from './briefWords'

const BRIEF = [
  '1. Land two Q4 funder commitments — hard dates: 2026-10-20 — source: goals sheet',
  '2. IDM keynote lands: live demo + one field story — hard dates: 2026-10-15 — source: calendar',
  '3. Fleet runs unattended — hard dates: none — source: goals sheet',
  'Not now: new product lines',
].join('\n')

describe('the brief', () => {
  it('reads the numbered lines, their dates and source, and the not-now line', () => {
    expect(briefItems(BRIEF)).toEqual([
      { n: 1, text: 'Land two Q4 funder commitments', dates: '2026-10-20', source: 'goals sheet' },
      { n: 2, text: 'IDM keynote lands: live demo + one field story', dates: '2026-10-15', source: 'calendar' },
      { n: 3, text: 'Fleet runs unattended', dates: 'none', source: 'goals sheet' },
    ])
    expect(briefNotNow(BRIEF)).toBe('new product lines')
    expect(briefItems('')).toEqual([])
    expect(briefItems(undefined)).toEqual([])
  })

  it('takes a priority number however it was written', () => {
    expect([2, '2', '#2', '2. IDM', 'priority 3'].map(priorityNumber)).toEqual([2, 2, 2, 2, 3])
    expect([2.5, 'Connect funder pipeline', null, {}].map(priorityNumber)).toEqual([null, null, null, null])
  })
})

describe('an idea', () => {
  it('says which brief priority it serves — or, from an older huddle, the priority it quoted', () => {
    expect(servesWords(1, BRIEF)).toEqual({ label: 'Serves priority 1', text: 'Land two Q4 funder commitments' })
    expect(servesWords(7, BRIEF)).toEqual({ label: 'Serves priority 7', text: '' })
    expect(servesWords(2, '')).toEqual({ label: 'Serves priority 2', text: '' })
    expect(servesWords('Connect funder pipeline', BRIEF)).toEqual({ label: 'For the priority', text: 'Connect funder pipeline' })
    expect(servesWords('', BRIEF)).toBeNull()
  })

  it('says what it needs from Jonathan, and nothing when it needs nothing', () => {
    expect(costWords({ kind: 'time', detail: '20-minute dry run Tuesday' })).toBe('20-minute dry run Tuesday')
    expect(costWords({ kind: 'decision', detail: '' })).toBe('a decision from you')
    expect(costWords({ kind: 'none', detail: 'n/a' })).toBe('')
    expect(costWords(undefined)).toBe('')
  })

  it('says what would make it fail, once', () => {
    expect(failsWords('the demo env flakes on stage.')).toBe('Would fail if the demo env flakes on stage')
    expect(failsWords('It would fail if Gates says no')).toBe('Would fail if Gates says no')
    expect(failsWords('')).toBe('')
  })
})

describe('a report', () => {
  it('reads a lever as a line naming the priority, the kind and whether it was checked', () => {
    expect(leverWords({ priority: 2, move: 'Rehearse the live demo', kind: 'new', task: '', blocked_by: '', verified: true }, BRIEF))
      .toBe('Priority 2 (IDM keynote lands: live demo + one field story): Rehearse the live demo — new work · checked')
    expect(leverWords({ priority: 3, move: 'Clear alarm noise', kind: 'unblock', task: 'T51', blocked_by: 'Hal', verified: false }, ''))
      .toBe('Priority 3: Clear alarm noise — unblocks T51 · blocked by Hal · not checked yet')
  })

  it('reads a need as who it is from, and an old string need as itself', () => {
    expect(needWords({ from: 'jonathan', ask: 'Pick the demo flow' })).toBe('From Jonathan: Pick the demo flow')
    expect(needWords('a page from Echo')).toBe('a page from Echo')
    expect(reportItemWords('state', 'T41 half done', BRIEF)).toBe('T41 half done')
    expect(reportItemWords('needs', { from: 'echo', ask: 'a story' }, BRIEF)).toBe('From Echo: a story')
  })
})
