// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'

import { parseOrigins } from './ConnectedAppsPage'

/**
 * The URL box is the field this page exists for, and the one nobody gets right
 * first time: people paste from an address bar (trailing slash), paste several
 * at once (commas), and press enter between them (newlines). Refusing any of
 * those would be pedantry that reads as "the site would not connect".
 */
describe('reading the URLs someone actually pastes', () => {
  it('takes one per line', () => {
    expect(parseOrigins('https://a.test\nhttps://b.test')).toEqual([
      'https://a.test',
      'https://b.test',
    ])
  })

  it('takes commas too', () => {
    expect(parseOrigins('https://a.test, https://b.test')).toEqual([
      'https://a.test',
      'https://b.test',
    ])
  })

  it('trims the trailing slash an address bar gives you', () => {
    // `https://host/` is not a valid `frame-ancestors` entry, and it is exactly
    // what a copy-paste produces.
    expect(parseOrigins('https://a.test/')).toEqual(['https://a.test'])
  })

  it('ignores blank lines rather than sending empty strings', () => {
    expect(parseOrigins('\n\nhttps://a.test\n\n')).toEqual(['https://a.test'])
  })

  it('is empty for empty input, so the server sees an explicit no-URLs', () => {
    expect(parseOrigins('   ')).toEqual([])
  })
})
