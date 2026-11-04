import { describe, expect, it } from 'vitest'
import { describeLine, describeLines } from './connectionLines'

describe('describeLine', () => {
  it.each([
    [{ method: 'GET', path: 'sobjects/Lead/*' }, 'Read Leads'],
    [{ method: 'GET', path: 'sobjects/Lead' }, 'Read Leads'],
    [{ method: 'GET', path: 'sobjects/Opportunity/*' }, 'Read Opportunities'],
    [{ method: 'GET', path: 'sobjects/Account/*' }, 'Read Accounts'],
    [{ method: 'PATCH', path: 'sobjects/Lead/*' }, 'Change Leads'],
    [{ method: 'POST', path: 'sobjects/Lead' }, 'Change Leads'],
    [{ method: 'DELETE', path: 'sobjects/Contact/*' }, 'Change Contacts'],
    [{ method: 'POST', path: 'actions/custom/flow/Create_Lead_From_Contact' }, 'Run the flow Create_Lead_From_Contact'],
    [{ method: 'GET', path: 'query' }, 'Search Salesforce records'],
    [{ method: 'GET', path: 'search' }, 'Search Salesforce records'],
  ])('%j reads as %s', (line, text) => {
    expect(describeLine(line)).toBe(text)
  })

  it('falls back to the method and path for anything else', () => {
    expect(describeLine({ method: 'GET', path: 'analytics/dashboards/*' })).toBe('GET analytics/dashboards/*')
    expect(describeLine({ method: 'POST', path: 'apexrest/Thing' })).toBe('POST apexrest/Thing')
  })

  it('describeLines lists each once', () => {
    expect(describeLines([{ method: 'GET', path: 'sobjects/Lead/*' }, { method: 'GET', path: 'sobjects/Lead' }])).toEqual(['Read Leads'])
  })
})
