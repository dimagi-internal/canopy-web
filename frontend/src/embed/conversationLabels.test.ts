import { describe, expect, it } from 'vitest'

import { agentLabel, conversationFacts, pageLabel, resourceLabel, whenLabel } from './conversationLabels'

describe('agentLabel', () => {
  it('is the name when the agent has one of its own', () => {
    expect(agentLabel({ slug: 'hal', name: 'Hal' })).toBe('Hal')
    expect(agentLabel({ slug: 'labsdata', name: 'Data agent' })).toBe('Data agent')
  })
  it('reads a slug-for-a-name as a word', () => {
    expect(agentLabel({ slug: 'labsdata', name: 'labsdata' })).toBe('Labsdata')
    expect(agentLabel({ slug: 'labsdata', name: '' })).toBe('Labsdata')
  })
})

describe('resourceLabel', () => {
  it.each([
    ['labs-data-space://12', 'Data space 12'],
    ['labs-data://', 'Data page'],
    ['labs-data://opportunities/9711,9712', 'Data · opportunities'],
    ['labs-marketplace://orgs', 'Marketplace · orgs'],
    ['canopy://agents/echo', 'Canopy · agents'],
    ['labs-data-space://12?filter=bob', 'Data space 12'],
    ['not a uri', ''],
    ['', ''],
  ])('%s → %s', (uri, label) => {
    expect(resourceLabel(uri)).toBe(label)
  })
})

describe('pageLabel', () => {
  it("prefers the page's own title, else its resource", () => {
    expect(pageLabel({ resource: 'labs-data-space://12', title: 'KMC — programme review' })).toBe('KMC — programme review')
    expect(pageLabel({ resource: 'labs-data-space://12' })).toBe('Data space 12')
    expect(pageLabel(null)).toBe('')
  })
})

describe('whenLabel and conversationFacts', () => {
  const now = new Date(2026, 9, 25, 15, 0)
  it('says today, yesterday, or the date', () => {
    expect(whenLabel(new Date(2026, 9, 25, 14, 14).toISOString(), now)).toMatch(/^2:14\s?pm today$/)
    expect(whenLabel(new Date(2026, 9, 24, 21, 50).toISOString(), now)).toMatch(/^yesterday 9:50\s?pm$/)
    expect(whenLabel(new Date(2026, 9, 20, 9, 5).toISOString(), now)).toMatch(/^Oct 20, 9:05\s?am$/)
    expect(whenLabel('nope', now)).toBe('')
  })
  it('joins when and where, and marks this page', () => {
    const at = new Date(2026, 9, 25, 14, 14).toISOString()
    expect(conversationFacts({ startedAt: at, origin: 'labs-data-space://12' }, 'labs-data-space://12', now))
      .toMatch(/^Started 2:14\s?pm today · on this page$/)
    expect(conversationFacts({ startedAt: at, origin: 'labs-data-space://7' }, 'labs-data-space://12', now))
      .toMatch(/· on Data space 7$/)
    expect(conversationFacts({ startedAt: at, origin: '' }, '', now)).toMatch(/^Started 2:14\s?pm today$/)
  })
})
