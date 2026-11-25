import { describe, expect, it } from 'vitest'
import { matchesAny, matchesPattern } from './matchPattern'

// The same cases as apps/workspace_apps/tests/test_browser_extension.py::test_matching_is_chromes_rule,
// so the panel and the server answer alike.
describe('matchesPattern', () => {
  it.each([
    ['https://mail.google.com/mail/u/0/#inbox', true],
    ['https://mail.google.com/', true],
    ['https://MAIL.google.com/mail?x=1', true],
    ['http://mail.google.com/mail/', false],
    ['https://mail.google.com.evil.com/', false],
    ['https://evil.com/?https://mail.google.com/', false],
    ['https://mail.google.com:8443/mail/', false],
    ['https://user:pw@mail.google.com/mail/', false],
    ['not a url', false],
  ])('%s → %s', (url, hit) => {
    expect(matchesPattern('https://mail.google.com/*', url)).toBe(hit)
  })

  it('matches the path and query, never the fragment', () => {
    expect(matchesPattern('https://mail.google.com/mail/*', 'https://mail.google.com/mail/u/0')).toBe(true)
    expect(matchesPattern('https://mail.google.com/mail/*', 'https://mail.google.com/other#/mail/')).toBe(false)
    expect(matchesPattern('https://mail.google.com/*inbox*', 'https://mail.google.com/x?view=inbox')).toBe(true)
  })

  it('a subdomain pattern covers the domain and its subdomains only', () => {
    expect(matchesPattern('https://*.example.com/*', 'https://a.example.com/')).toBe(true)
    expect(matchesPattern('https://*.example.com/*', 'https://example.com/')).toBe(true)
    expect(matchesPattern('https://*.example.com/*', 'https://badexample.com/')).toBe(false)
  })

  it('refuses a pattern it would not have validated', () => {
    for (const p of ['http://mail.google.com/*', 'https://*/*', '<all_urls>', '']) {
      expect(matchesPattern(p, 'https://mail.google.com/')).toBe(false)
    }
    expect(matchesAny(['https://mail.google.com/*'], null)).toBe(false)
  })
})
