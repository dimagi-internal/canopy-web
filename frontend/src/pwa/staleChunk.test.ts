// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { browser, isStaleChunkError, lazyRoute, recoverFromStaleChunk } from './staleChunk'

const NOW = 1_000_000

beforeEach(() => {
  sessionStorage.clear()
})

describe('recognising a chunk that is gone', () => {
  const chunkErrors = [
    'Failed to fetch dynamically imported module: https://labs.connect.dimagi.com/canopy/assets/ItemsSection-Bneq-YjE.js',
    'error loading dynamically imported module',
    'Importing a module script failed.',
    'Failed to load module script: Expected a JavaScript module script but the server responded with a MIME type of "text/html".',
  ]
  for (const message of chunkErrors) {
    it(message.slice(0, 48), () => {
      expect(isStaleChunkError(new Error(message))).toBe(true)
    })
  }

  it('leaves ordinary render errors alone — they must reach the boundary', () => {
    // This is the important negative: reloading on a real bug would hide it and
    // could loop the user through the same crash.
    expect(isStaleChunkError(new Error("Cannot read properties of undefined (reading 'map')"))).toBe(
      false,
    )
    expect(isStaleChunkError(new TypeError('fetch failed'))).toBe(false)
    expect(isStaleChunkError(undefined)).toBe(false)
    expect(isStaleChunkError('some string')).toBe(false)
  })
})

describe('deciding whether to reload', () => {
  const chunkError = new Error('Failed to fetch dynamically imported module: /assets/x-AAAA1111.js')

  it('reloads once when a chunk is gone and we are online', () => {
    const reload = vi.fn()
    expect(recoverFromStaleChunk(chunkError, { online: true, now: NOW, reload })).toBe(true)
    expect(reload).toHaveBeenCalledOnce()
  })

  it('does not reload twice inside the cooldown — that is the loop guard', () => {
    const reload = vi.fn()
    recoverFromStaleChunk(chunkError, { online: true, now: NOW, reload })
    expect(recoverFromStaleChunk(chunkError, { online: true, now: NOW + 2_000, reload })).toBe(false)
    expect(reload).toHaveBeenCalledOnce()
  })

  it('reloads again for a later deploy, once the cooldown has passed', () => {
    const reload = vi.fn()
    recoverFromStaleChunk(chunkError, { online: true, now: NOW, reload })
    expect(recoverFromStaleChunk(chunkError, { online: true, now: NOW + 3_600_000, reload })).toBe(
      true,
    )
    expect(reload).toHaveBeenCalledTimes(2)
  })

  it('never reloads offline — the chunk is missing because there is no network', () => {
    const reload = vi.fn()
    expect(recoverFromStaleChunk(chunkError, { online: false, now: NOW, reload })).toBe(false)
    expect(reload).not.toHaveBeenCalled()
  })

  it('never reloads for a non-chunk error', () => {
    const reload = vi.fn()
    const bug = new Error("Cannot read properties of undefined (reading 'map')")
    expect(recoverFromStaleChunk(bug, { online: true, now: NOW, reload })).toBe(false)
    expect(reload).not.toHaveBeenCalled()
  })
})

describe('lazyRoute', () => {
  it('passes the module through untouched when the chunk loads', async () => {
    const mod = { default: 'Section' }
    await expect(lazyRoute(async () => mod)()).resolves.toBe(mod)
  })

  it('clears the stamp on success, so a later deploy gets its own reload', async () => {
    sessionStorage.setItem('canopy:stale-chunk-reload-at', String(Date.now()))
    await lazyRoute(async () => ({ default: 'Section' }))()
    expect(sessionStorage.getItem('canopy:stale-chunk-reload-at')).toBeNull()
  })

  it('rethrows a real error so the route boundary still renders', async () => {
    const bug = new Error('boom')
    await expect(lazyRoute(async () => Promise.reject(bug))()).rejects.toBe(bug)
  })

  it('hangs rather than resolving while the page is being replaced', async () => {
    // Resolving or rejecting during the reload would race the navigation and
    // flash the error boundary on the way out.
    const reload = vi.spyOn(browser, 'reload').mockImplementation(() => {})

    const pending = lazyRoute(async () =>
      Promise.reject(new Error('Failed to fetch dynamically imported module: /assets/a-BBBB2222.js')),
    )()
    const settled = await Promise.race([
      pending.then(() => 'settled'),
      new Promise((r) => setTimeout(() => r('still pending'), 20)),
    ])

    expect(reload).toHaveBeenCalledOnce()
    expect(settled).toBe('still pending')
    vi.restoreAllMocks()
  })
})
