import { describe, expect, it } from 'vitest'

import { buildPageContext, describePage } from './pageContext'

/**
 * The use case that drives these tests: "this whole feature set should go"
 * needs only the route layer, on ANY page, including ones nobody wrote a rule
 * for.
 *
 * The page layer that used to live here — a contributor registry merged in
 * under `onScreen` — moved to `pageState.ts`, and its tests moved with it.
 * Every behaviour they pinned (last-wins, a superseded disposer not clearing
 * the live page, a throwing contributor costing only itself) is asserted in
 * `pageState.test.ts` against the channel that replaced it.
 */

describe('the route layer', () => {
  it('names an agent rail section specifically, since that is where staleness is noticed', () => {
    expect(describePage('/w/connect/agents/echo/items')).toEqual({
      surface: 'the items view of agent echo',
      params: { workspace: 'connect', agent: 'echo', section: 'items' },
    })
  })

  it('falls back to the agent workspace when there is no section', () => {
    expect(describePage('/w/connect/agents/echo').surface).toBe('the agent workspace for echo')
  })

  it('puts the specific rule first — a section must not be read as the workspace', () => {
    // Ordering is the whole contract of an ordered rule list, and getting it
    // backwards is silent: you would still get a plausible-looking answer.
    expect(describePage('/w/connect/agents/echo/turns').params?.section).toBe('turns')
  })

  it('distinguishes a DDD run from its narrative', () => {
    expect(describePage('/w/connect/ddd/my-story/run-7').params).toEqual({
      workspace: 'connect',
      narrative: 'my-story',
      run: 'run-7',
    })
    expect(describePage('/w/connect/ddd/my-story').params).toEqual({
      workspace: 'connect',
      narrative: 'my-story',
    })
  })

  it('describes the workbench at the bare workspace root', () => {
    expect(describePage('/w/connect').surface).toBe('the project workbench')
    expect(describePage('/w/connect/').surface).toBe('the project workbench')
  })

  it('covers the personal surfaces', () => {
    expect(describePage('/supervisor').surface).toBe('the supervisor inbox')
    expect(describePage('/insights').surface).toBe('the cross-portfolio insights feed')
  })

  it('still says something for a page with no rule', () => {
    // Opt-OUT, unlike presence: an agent that cannot say what you are looking
    // at is the failure this exists to prevent, so an unmatched page still
    // carries its path.
    const ctx = buildPageContext('/some/new/page')
    expect(ctx.surface).toBe('a canopy page')
    expect(ctx.path).toBe('/some/new/page')
  })
})
