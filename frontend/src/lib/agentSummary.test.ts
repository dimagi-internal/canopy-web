import { describe, expect, it } from 'vitest'
import { agentSummary } from './agentSummary'

describe('agentSummary', () => {
  it('drops the name lead and the operating-model sentence of a factory description', () => {
    expect(
      agentSummary(
        'Sift',
        'Sift — Manage the Connect roadmap and backlogs Built on the canopy agent operating model: a turn-driven persona with reads-free / writes-gated guarding.',
      ),
    ).toBe('Manage the Connect roadmap and backlogs')
  })

  it('matches the name case-insensitively', () => {
    expect(agentSummary('lex', 'lex — Lilianna’s personal agent')).toBe('Lilianna’s personal agent')
    expect(agentSummary('Otto', 'otto — develops connect-hub')).toBe('develops connect-hub')
  })

  it('leaves a hand-written description alone', () => {
    const d = 'Connect division assistant. Can update the website and supports operational tasks.'
    expect(agentSummary('Jarvis', d)).toBe(d)
  })

  it('returns an empty string for no description', () => {
    expect(agentSummary('Echo', '')).toBe('')
  })
})
