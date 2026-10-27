import { describe, expect, it } from 'vitest'
// @ts-expect-error -- a plain .mjs build script, no type declarations
import { isAssetPath, manifestFiles, nextHistory, KEEP_DAYS } from './carryAssets.mjs'

const NOW = 1_800_000_000
const DAY = 86_400

describe('nextHistory', () => {
  it('stamps this build now and keeps recent earlier assets it holds', () => {
    const h = nextHistory({
      current: ['assets/index-NEW00000.js'],
      previous: { 'assets/index-OLD00000.js': NOW - DAY },
      carried: new Set(['assets/index-OLD00000.js']),
      now: NOW,
    })
    expect(h.files).toEqual({
      'assets/index-NEW00000.js': NOW,
      'assets/index-OLD00000.js': NOW - DAY,
    })
  })

  it('drops an asset no live build has used for longer than the window', () => {
    const old = 'assets/index-OLD00000.js'
    const h = nextHistory({
      current: [],
      previous: { [old]: NOW - (KEEP_DAYS + 1) * DAY },
      carried: new Set([old]),
      now: NOW,
    })
    expect(h.files).toEqual({})
  })

  it('drops an entry whose file could not be fetched', () => {
    const h = nextHistory({
      current: [],
      previous: { 'assets/gone-AAAAAAAA.js': NOW },
      carried: new Set(),
      now: NOW,
    })
    expect(h.files).toEqual({})
  })

  it('refreshes the stamp of an unchanged chunk this build still ships', () => {
    const same = 'assets/bits-CdY1RkYV.js'
    const h = nextHistory({ current: [same], previous: { [same]: NOW - 6 * DAY }, carried: new Set(), now: NOW })
    expect(h.files[same]).toBe(NOW)
  })
})

describe('isAssetPath', () => {
  it('accepts only a single name under assets/', () => {
    expect(isAssetPath('assets/index-B8WCLOOd.js')).toBe(true)
    expect(isAssetPath('assets/../config.py')).toBe(false)
    expect(isAssetPath('assets/a/b.js')).toBe(false)
    expect(isAssetPath('index.html')).toBe(false)
    expect(isAssetPath('/etc/passwd')).toBe(false)
  })
})

describe('manifestFiles', () => {
  it('collects entry files, css and referenced assets', () => {
    const files = manifestFiles({
      'index.html': { file: 'assets/index-AAAAAAAA.js', css: ['assets/src-BBBBBBBB.css'] },
      'font.woff2': { file: 'assets/font-CCCCCCCC.woff2', assets: ['assets/x-DDDDDDDD.png'] },
    })
    expect([...files].sort()).toEqual([
      'assets/font-CCCCCCCC.woff2',
      'assets/index-AAAAAAAA.js',
      'assets/src-BBBBBBBB.css',
      'assets/x-DDDDDDDD.png',
    ])
  })
})
