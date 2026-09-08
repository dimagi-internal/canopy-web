import { describe, expect, it, vi } from 'vitest'
import { createHiddenGate } from './applyWhenHidden'

/** A document/window pair whose visibility and events the test drives. */
function fakeHost(initial: 'visible' | 'hidden' = 'visible') {
  const listeners: Record<string, Set<() => void>> = {}
  const on = (type: string, fn: () => void) => {
    ;(listeners[type] ??= new Set()).add(fn)
  }
  const off = (type: string, fn: () => void) => {
    listeners[type]?.delete(fn)
  }
  const host = {
    visibilityState: initial as 'visible' | 'hidden',
    addEventListener: on as Document['addEventListener'],
    removeEventListener: off as Document['removeEventListener'],
  }
  return {
    doc: host,
    win: { addEventListener: on, removeEventListener: off } as unknown as Window,
    /** How many listeners are still attached — the leak check. */
    count: (type: string) => listeners[type]?.size ?? 0,
    fire: (type: string) => [...(listeners[type] ?? [])].forEach((fn) => fn()),
    hide: () => {
      host.visibilityState = 'hidden'
    },
    show: () => {
      host.visibilityState = 'visible'
    },
  }
}

const gateFor = (h: ReturnType<typeof fakeHost>) =>
  createHiddenGate({ doc: h.doc, win: h.win })

describe('createHiddenGate', () => {
  it('waits while the page is visible', () => {
    const h = fakeHost('visible')
    const apply = vi.fn()
    gateFor(h)(apply)
    expect(apply).not.toHaveBeenCalled()
  })

  it('applies as soon as the page hides', () => {
    const h = fakeHost('visible')
    const apply = vi.fn()
    gateFor(h)(apply)
    h.hide()
    h.fire('visibilitychange')
    expect(apply).toHaveBeenCalledTimes(1)
  })

  it('applies immediately if the page is already hidden', () => {
    const h = fakeHost('hidden')
    const apply = vi.fn()
    gateFor(h)(apply)
    expect(apply).toHaveBeenCalledTimes(1)
  })

  it('ignores a visibilitychange that did not end in hidden', () => {
    // Tab focus moving between visible states must not trigger a reload.
    const h = fakeHost('visible')
    const apply = vi.fn()
    gateFor(h)(apply)
    h.fire('visibilitychange')
    expect(apply).not.toHaveBeenCalled()
  })

  it('applies on pagehide, which iOS and bfcache deliver without a visibility change', () => {
    const h = fakeHost('visible')
    const apply = vi.fn()
    gateFor(h)(apply)
    h.fire('pagehide')
    expect(apply).toHaveBeenCalledTimes(1)
  })

  it('applies once however many updates arrive first', () => {
    // The regression this latch exists for: one `onNeedRefresh` per deploy, and
    // six deploys in a day. Without it, six listeners survived to the same hide
    // and each asked for its own reload.
    const h = fakeHost('visible')
    const apply = vi.fn()
    const gate = gateFor(h)
    gate(apply)
    gate(apply)
    gate(apply)
    h.hide()
    h.fire('visibilitychange')
    expect(apply).toHaveBeenCalledTimes(1)
  })

  it('applies once when both signals fire', () => {
    const h = fakeHost('visible')
    const apply = vi.fn()
    gateFor(h)(apply)
    h.hide()
    h.fire('visibilitychange')
    h.fire('pagehide')
    expect(apply).toHaveBeenCalledTimes(1)
  })

  it('leaves no listener attached once it has fired', () => {
    const h = fakeHost('visible')
    gateFor(h)(() => {})
    expect(h.count('visibilitychange') + h.count('pagehide')).toBe(2)
    h.hide()
    h.fire('visibilitychange')
    expect(h.count('visibilitychange') + h.count('pagehide')).toBe(0)
  })

  it('does nothing where there is no document — SSR, or a worker', () => {
    const apply = vi.fn()
    createHiddenGate({ doc: undefined, win: undefined })(apply)
    expect(apply).not.toHaveBeenCalled()
  })
})
