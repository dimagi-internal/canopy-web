import { describe, expect, it } from 'vitest'
import { describeLine, describeLines, lineWords } from './connectionLines'

describe('describeLine', () => {
  it.each([
    [{ method: 'GET', path: 'sobjects/Lead/*' }, 'Read Leads'],
    [{ method: 'GET', path: 'sobjects/Lead' }, 'Read Leads'],
    [{ method: 'GET', path: 'sobjects/Opportunity/*' }, 'Read Opportunities'],
    [{ method: 'GET', path: 'sobjects/Account/*' }, 'Read Accounts'],
    [{ method: 'GET', path: 'sobjects/*/*' }, 'Read any record'],
    [{ method: 'PATCH', path: 'sobjects/Lead/*' }, 'Change Leads'],
    [{ method: 'POST', path: 'sobjects/Lead' }, 'Create Leads'],
    [{ method: 'POST', path: 'sobjects/Contact' }, 'Create Contacts'],
    [{ method: 'DELETE', path: 'sobjects/Contact/*' }, 'Delete Contacts'],
    [{ method: 'POST', path: 'actions/custom/flow/Create_Lead_From_Contact' }, 'Run the flow Create_Lead_From_Contact'],
    [{ method: 'POST', path: 'apexrest/Thing' }, 'Run the Salesforce action Thing'],
    [{ method: 'GET', path: 'query' }, 'Search any record you can see'],
    [{ method: 'GET', path: 'search' }, 'Search any record you can see'],
  ])('%j reads as %s', (line, text) => {
    expect(describeLine(line)).toBe(text)
  })

  it('says whose records a search reaches', () => {
    expect(describeLine({ method: 'GET', path: 'query' }, 'eva')).toBe('Search any record eva can see')
  })

  it('anything it does not recognise says so and shows the call as code', () => {
    expect(lineWords({ method: 'GET', path: 'analytics/dashboards/*' })).toEqual({
      words: 'Other Salesforce access:', code: 'GET analytics/dashboards/*',
    })
    expect(describeLine({ method: 'GET', path: 'limits' })).toBe('Other Salesforce access: GET limits')
  })
})

describe('describeLines', () => {
  const shown = (over: Parameters<typeof describeLines>[0], who?: string) => describeLines(over, who).map((l) => l.words)

  it('lists each once', () => {
    expect(shown({ reads: [{ method: 'GET', path: 'sobjects/Lead/*' }, { method: 'GET', path: 'sobjects/Lead' }] })).toEqual(['Read Leads'])
  })

  it('keeps deleting apart from changing, and lists deletes first, then other writes, then reads', () => {
    expect(shown({
      reads: [{ method: 'GET', path: 'sobjects/Contact/*' }, { method: 'GET', path: 'query' }],
      writes: [{ method: 'PATCH', path: 'sobjects/Contact/*' }, { method: 'POST', path: 'sobjects/Contact' }, { method: 'DELETE', path: 'sobjects/Contact/*' }],
    }, 'eva')).toEqual(['Delete Contacts', 'Change Contacts', 'Create Contacts', 'Read Contacts', 'Search any record eva can see'])
  })
})
