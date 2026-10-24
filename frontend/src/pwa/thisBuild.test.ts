// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { swIsThisBuild, thisPageEntry } from './thisBuild'

function docWith(html: string): Document {
  return new DOMParser().parseFromString(`<html><head>${html}</head></html>`, 'text/html')
}

// The shape vite-plugin-pwa emits: one precache entry per hashed asset.
const SW = 'e.precacheAndRoute([{url:"assets/index-BBFp3NYI.js",revision:null},{url:"index.html",revision:"66"}])'

describe('thisPageEntry', () => {
  it("names the page's hashed entry script", () => {
    expect(thisPageEntry(docWith('<script type="module" crossorigin src="/assets/index-BBFp3NYI.js"></script>'))).toBe(
      'index-BBFp3NYI.js',
    )
  })

  it('follows a path-prefixed deployment', () => {
    expect(thisPageEntry(docWith('<script type="module" src="/canopy/assets/index-Zz9.js"></script>'))).toBe('index-Zz9.js')
  })

  it('is null in dev, where no hashed entry exists', () => {
    expect(thisPageEntry(docWith('<script type="module" src="/src/main.tsx"></script>'))).toBeNull()
  })
})

describe('swIsThisBuild', () => {
  it("is true when the server's sw.js precaches this page's entry", () => {
    expect(swIsThisBuild(SW, 'index-BBFp3NYI.js')).toBe(true)
  })

  it('is false after a deploy renamed the entry', () => {
    expect(swIsThisBuild(SW, 'index-OLDhash1.js')).toBe(false)
  })

  it('is false when the entry is unknown, so the reload is still offered', () => {
    expect(swIsThisBuild(SW, null)).toBe(false)
  })
})
