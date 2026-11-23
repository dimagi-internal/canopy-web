// A recurring task as a list says it: the rhythm chip, the sentence on its page,
// and a due date as a person says it.
import { describe, expect, it } from 'vitest'

import { dueLabel, isOverdue, rhythmLabel } from './recurring'

describe('rhythmLabel', () => {
  it.each([
    [{ every: 'week', weekday: 'wed', auto: true }, 'Every Wed', 'every Wednesday'],
    [{ every: 'week', auto: false }, 'Weekly', 'every week'],
    [{ every: '2weeks', weekday: 'sun', auto: true }, 'Every other Sun', 'every other Sunday'],
    [{ every: 'day', auto: false }, 'Daily', 'every day'],
    [{ every: 'weekdays', auto: true }, 'Weekdays', 'every weekday'],
    [{ every: 'month', auto: true }, 'Monthly', 'every month'],
    [{ every: 'as_needed', auto: false }, 'As needed', 'as needed'],
  ] as const)('%o reads %s', (rec, chip, sentence) => {
    expect(rhythmLabel(rec)).toBe(chip)
    expect(rhythmLabel(rec, true)).toBe(sentence)
  })
})

describe('dueLabel', () => {
  const today = new Date(2026, 10, 18) // Wed Nov 18 2026
  it('says today, tomorrow, a weekday this week, else the date', () => {
    expect(dueLabel('2026-11-18', today)).toBe('Today')
    expect(dueLabel('2026-11-19', today)).toBe('Tomorrow')
    expect(dueLabel('2026-11-21', today)).toBe('Sat')
    expect(dueLabel('2026-11-30', today)).toBe('Nov 30')
    expect(dueLabel('2026-11-10', today)).toBe('Nov 10')
    expect(dueLabel('2027-01-04', today)).toBe('Jan 4, 2027')
  })
  it('knows what is past due', () => {
    expect(isOverdue('2026-11-17', today)).toBe(true)
    expect(isOverdue('2026-11-18', today)).toBe(false)
  })
})
