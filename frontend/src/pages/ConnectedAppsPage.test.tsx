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

import { parseKeys } from './ConnectedAppsPage'

/**
 * Several keys at once is the normal state during a rotation — publish the new
 * one beside the old, switch the signer, remove the old — so the box has to
 * take more than one without being fussy about how they are separated.
 */
describe('reading pasted signing keys', () => {
  const A = '-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----'
  const B = '-----BEGIN PUBLIC KEY-----\nBBBB\n-----END PUBLIC KEY-----'

  it('takes one', () => {
    expect(parseKeys(A)).toEqual([A])
  })

  it('takes two pasted back to back', () => {
    expect(parseKeys(`${A}\n${B}`)).toEqual([A, B])
  })

  it('tolerates the surrounding whitespace a paste brings', () => {
    expect(parseKeys(`\n\n  ${A}  \n\n`)).toEqual([A])
  })

  it('is empty for empty input, so the server sees an explicit none', () => {
    expect(parseKeys('   ')).toEqual([])
  })

  it('ignores trailing junk that is not a key', () => {
    expect(parseKeys(`${A}\nthanks!`)).toEqual([A])
  })
})
