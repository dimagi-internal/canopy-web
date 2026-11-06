import { describe, expect, it } from 'vitest'
import { siteLabel } from './siteLabel'

describe('siteLabel', () => {
  it('reads a registered site name as words', () => {
    expect(siteLabel('connect-labs')).toBe('Connect Labs')
    expect(siteLabel('canopy-web')).toBe('Canopy Web')
    expect(siteLabel('ace_web')).toBe('Ace Web')
  })
  it('never prints nothing', () => {
    expect(siteLabel('')).toBe('this site')
  })
})
