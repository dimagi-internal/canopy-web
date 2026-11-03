import { describe, expect, it } from 'vitest'
import { agentSummary } from './agentSummary'

// Verbatim from canopy packages/canopy_agent_factory/canopy_agent_factory/_factory.py
// (`_PLUGIN_JSON` "description"). Keep in step with that template.
const FACTORY_TEMPLATE =
  '{{AGENT_NAME}} — {{MANDATE}} Built on the canopy agent operating model: a turn-driven persona with reads-free / writes-gated guarding, with outbound actions that follow the turn mode.'

const fromFactory = (name: string, mandate: string) =>
  FACTORY_TEMPLATE.replace('{{AGENT_NAME}}', name).replace('{{MANDATE}}', mandate)

describe('agentSummary', () => {
  it('reduces the exact factory template to the mandate', () => {
    expect(agentSummary('Neo', fromFactory('Neo', 'Creates work orders, drafts plans, and prepares high-gloss reports'))).toBe(
      'Creates work orders, drafts plans, and prepares high-gloss reports',
    )
  })

  it('handles the older factory wording (human-approved outbound actions)', () => {
    expect(
      agentSummary(
        'Sift',
        'Sift — Manage the Connect roadmap and backlogs Built on the canopy agent operating model: a turn-driven persona with reads-free / writes-gated guarding and human-approved outbound actions.',
      ),
    ).toBe('Manage the Connect roadmap and backlogs')
  })

  it('matches the name case-insensitively', () => {
    expect(agentSummary('Otto', 'otto — develops connect-hub')).toBe('develops connect-hub')
  })

  it('accepts an en dash or hyphen as the lead separator', () => {
    expect(agentSummary('Lex', 'Lex – personal agent')).toBe('personal agent')
    expect(agentSummary('Lex', 'Lex - personal agent')).toBe('personal agent')
  })

  it('keeps a name that is not a lead (no separator after it)', () => {
    expect(agentSummary('Hal', 'Hal helps with canopy')).toBe('Hal helps with canopy')
  })

  it('strips the operating-model sentence even without a name lead', () => {
    expect(agentSummary('Ace', 'Runs opportunities. Built on the canopy agent operating model: x.')).toBe('Runs opportunities.')
  })

  it('is empty when the description is only boilerplate', () => {
    expect(agentSummary('Neo', fromFactory('Neo', '').replace('—  ', '— '))).toBe('')
  })

  it('leaves a hand-written description alone', () => {
    const d = 'Connect division assistant. Can update the website and supports operational tasks.'
    expect(agentSummary('Jarvis', d)).toBe(d)
  })

  it('returns an empty string for no description', () => {
    expect(agentSummary('Echo', '')).toBe('')
  })
})
