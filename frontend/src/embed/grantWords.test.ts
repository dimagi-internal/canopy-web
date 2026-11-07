import { describe, expect, it } from 'vitest'
import { scopePhrases, siteName } from './grantWords'

describe('grantWords', () => {
  it('prefers the name the site publishes, else reads the registered name as words', () => {
    expect(siteName({ site: 'connect-labs', site_title: 'Connect Labs (Dimagi)' })).toBe('Connect Labs (Dimagi)')
    expect(siteName({ site: 'connect-labs', site_title: '  ' })).toBe('Connect Labs')
    expect(siteName({ site: 'connect-labs' })).toBe('Connect Labs')
  })
  it('lists only the phrases the site gave', () => {
    expect(scopePhrases({ site: 's' })).toEqual([])
    expect(scopePhrases({ site: 's', scope_labels: [{ scope: 'a:read', label: 'read x' }, { scope: 'b', label: '' }] }))
      .toEqual(['read x'])
  })
})
