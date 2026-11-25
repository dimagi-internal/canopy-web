import { describe, expect, it } from 'vitest'
import { checkDownload, cleanParams, externalUrl, withParams } from './frameOut'

describe('externalUrl', () => {
  it('https only, no credentials', () => {
    expect(externalUrl('https://dimagi.my.salesforce.com/00Q1')).toBe('https://dimagi.my.salesforce.com/00Q1')
    for (const bad of ['http://x.org', 'javascript:alert(1)', 'data:text/html,x', 'blob:https://a/b', '/w/x', 'https://u:p@x.org', 42, null]) {
      expect(externalUrl(bad)).toBeNull()
    }
  })
})

describe('cleanParams', () => {
  it('strings, nulls dropped, draft reserved', () => {
    expect(cleanParams({ board: 5, q: 'a b', gone: null })).toEqual({ board: '5', q: 'a b' })
    expect(cleanParams({ draft: '3' })).toMatch(/canopy's own/)
    expect(cleanParams({ 'a b': 1 })).toMatch(/not a parameter name/)
    expect(cleanParams([])).toMatch(/object/)
    expect(cleanParams({ x: 'y'.repeat(2001) })).toMatch(/2000/)
  })
  it('withParams keeps draft and the hash', () => {
    expect(withParams('https://c.org/w/a/apps/s/kanban?draft=2&board=1#top', { board: '9' }))
      .toBe('https://c.org/w/a/apps/s/kanban?draft=2&board=9#top')
  })
})

describe('checkDownload', () => {
  it('a plain name and a harmless type', () => {
    const d = checkDownload('../../settings.json', 'application/json', '{}')
    expect(d).toEqual({ name: 'settings.json', type: 'application/json', data: '{}' })
    expect((checkDownload('x.html', 'text/html', '<b>') as { type: string }).type).toBe('application/octet-stream')
    expect((checkDownload('x.svg', 'image/svg+xml', '<svg/>') as { type: string }).type).toBe('application/octet-stream')
    expect((checkDownload('x.csv', 'nonsense', 'a,b') as { type: string }).type).toBe('application/octet-stream')
  })
  it('refusals', () => {
    expect(checkDownload('', 'text/plain', 'x')).toMatch(/file name/)
    expect(checkDownload('a.txt', 'text/plain', { a: 1 })).toMatch(/must be text/)
    expect(checkDownload('a.bin', 'application/octet-stream', new ArrayBuffer(26 * 1024 * 1024))).toMatch(/25 MB/)
  })
})
