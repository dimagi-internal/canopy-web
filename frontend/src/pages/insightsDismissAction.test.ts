import { describe, expect, it, vi } from 'vitest'

import { dismissInsightsAction, type DismissDeps } from './insightsDismissAction'

function deps(overrides: Partial<DismissDeps> = {}): DismissDeps & { applied: number[][] } {
  const applied: number[][] = []
  return {
    visible: new Set([1, 2, 3]),
    dismiss: async () => undefined,
    onDismissed: (ids) => applied.push(ids),
    applied,
    ...overrides,
  } as DismissDeps & { applied: number[][] }
}

describe('dismissing what the agent asked for', () => {
  it('refuses an id that is not on the page', async () => {
    await expect(dismissInsightsAction([1, 99], deps())).rejects.toThrow('not on this page: 99')
  })

  it('does not dismiss anything when one id is refused', async () => {
    const dismiss = vi.fn(async () => undefined)
    await expect(dismissInsightsAction([1, 99], deps({ dismiss }))).rejects.toThrow()
    expect(dismiss).not.toHaveBeenCalled()
  })

  it('fires the requests together, not one after another', async () => {
    // The budget is a 20s server-side timeout on a list the user calls stale.
    // Awaited in turn, a large clear-out exceeds it and the agent is told the
    // page is closed while the tab is still working.
    let inFlight = 0
    let peak = 0
    const dismiss = async () => {
      inFlight += 1
      peak = Math.max(peak, inFlight)
      await new Promise((r) => setTimeout(r, 5))
      inFlight -= 1
    }

    await dismissInsightsAction([1, 2, 3], deps({ dismiss }))

    expect(peak).toBe(3)
  })

  it('reports a partial failure as partial, and still applies what succeeded', async () => {
    // The old loop let the first rejection escape: the rows already deleted
    // server-side stayed on screen, and the agent was told it all failed.
    const dismiss = vi.fn(async (id: number) => {
      if (id === 2) throw new Error('boom')
    })
    const d = deps({ dismiss })

    await expect(dismissInsightsAction([1, 2, 3], d)).rejects.toThrow('dismissed 2 of 3')

    expect(d.applied).toEqual([[1, 3]])
  })

  it('returns what it dismissed on the happy path', async () => {
    const d = deps()
    await expect(dismissInsightsAction([1, 3], d)).resolves.toEqual({ dismissed: 2, ids: [1, 3] })
    expect(d.applied).toEqual([[1, 3]])
  })

  it('an empty list is a no-op, not an error', async () => {
    const d = deps()
    await expect(dismissInsightsAction([], d)).resolves.toEqual({ dismissed: 0, ids: [] })
    expect(d.applied).toEqual([])
  })
})
