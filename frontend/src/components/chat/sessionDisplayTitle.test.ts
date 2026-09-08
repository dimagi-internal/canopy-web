import { describe, expect, it } from 'vitest'
import { sessionDisplayTitle } from './sessionDisplayTitle'

describe('sessionDisplayTitle', () => {
  it('collapses the doubled resume marker seen in the live list', () => {
    expect(sessionDisplayTitle('c-resume-c-resume-ace-kmc-metrics-e999')).toBe(
      'c-resume-ace-kmc-metrics-e999',
    )
    expect(sessionDisplayTitle('c-resume-c-resume-feat-runtime-yaml-1f1e')).toBe(
      'c-resume-feat-runtime-yaml-1f1e',
    )
  })

  it('collapses a deeper nest to a single marker', () => {
    expect(sessionDisplayTitle('c-resume-c-resume-c-resume-spark-ec06')).toBe('c-resume-spark-ec06')
  })

  it('leaves a single resume alone', () => {
    expect(sessionDisplayTitle('c-resume-spark-ec06')).toBe('c-resume-spark-ec06')
  })

  it('leaves every other canopy name untouched', () => {
    for (const name of [
      'c-meet-our-speakers-aif-s-annual-65ba',
      'c-issue-triage-4a4e',
      'c-turn-0001',
    ]) {
      expect(sessionDisplayTitle(name)).toBe(name)
    }
  })

  it('does not rewrite a human-typed emdash task', () => {
    // No `c-` marker means a person named it; it is not ours to tidy.
    expect(sessionDisplayTitle('bednet')).toBe('bednet')
    expect(sessionDisplayTitle('userswitch')).toBe('userswitch')
    expect(sessionDisplayTitle('resume-resume-by-hand')).toBe('resume-resume-by-hand')
  })

  it('does not collapse a word that merely recurs later in the subject', () => {
    // Only an immediately repeated marker is a stack; "resume" appearing again
    // further along is part of the subject someone wrote.
    expect(sessionDisplayTitle('c-resume-plan-then-resume-later-ab12')).toBe(
      'c-resume-plan-then-resume-later-ab12',
    )
  })

  it('handles empty and missing input', () => {
    expect(sessionDisplayTitle('')).toBe('')
    expect(sessionDisplayTitle(null)).toBe('')
    expect(sessionDisplayTitle(undefined)).toBe('')
  })
})
