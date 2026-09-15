import { afterEach, describe, expect, it } from 'vitest'

import { buildPageContext, describePage, setPageContributor } from './pageContext'

/**
 * The two real use cases drive these tests:
 *
 *  - "this whole feature set should go" needs only the route layer, on ANY
 *    page, including ones nobody wrote a rule for.
 *  - "this inbox is stale" needs the page layer, with ages.
 */

let dispose: (() => void) | null = null
afterEach(() => {
  dispose?.()
  dispose = null
})

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

describe('the page layer', () => {
  it('adds what is on screen under onScreen', () => {
    dispose = setPageContributor(() => ({ open_item_count: 3 }))
    expect(buildPageContext('/w/connect/agents/echo/items')).toMatchObject({
      surface: 'the items view of agent echo',
      onScreen: { open_item_count: 3 },
    })
  })

  it('reads the contributor at build time, not at registration', () => {
    let count = 1
    dispose = setPageContributor(() => ({ count }))
    expect((buildPageContext('/x') as { onScreen: { count: number } }).onScreen.count).toBe(1)
    count = 2
    expect((buildPageContext('/x') as { onScreen: { count: number } }).onScreen.count).toBe(2)
  })

  it('last registration wins', () => {
    setPageContributor(() => ({ which: 'stale' }))
    dispose = setPageContributor(() => ({ which: 'current' }))
    expect(buildPageContext('/x')).toMatchObject({ onScreen: { which: 'current' } })
  })

  it('unregistering leaves the route layer intact', () => {
    // Navigating away must not leave the agent describing a page you have left
    // — but it must still know where you ARE.
    const off = setPageContributor(() => ({ gone: true }))
    off()
    const ctx = buildPageContext('/supervisor')
    expect(ctx).not.toHaveProperty('onScreen')
    expect(ctx.surface).toBe('the supervisor inbox')
  })

  it('a disposer from a superseded contributor does not clear the current one', () => {
    const offFirst = setPageContributor(() => ({ which: 'first' }))
    dispose = setPageContributor(() => ({ which: 'second' }))
    // React unmount order can fire the old component's cleanup after the new
    // one registered; clearing unconditionally would blank a live page.
    offFirst()
    expect(buildPageContext('/x')).toMatchObject({ onScreen: { which: 'second' } })
  })

  it('a throwing contributor costs only the page layer', () => {
    dispose = setPageContributor(() => {
      throw new Error('page bug')
    })
    const ctx = buildPageContext('/w/connect/agents/echo/items')
    expect(ctx).not.toHaveProperty('onScreen')
    expect(ctx.surface).toBe('the items view of agent echo')
  })
})

describe('the URL is the cheapest context there is', () => {
  it('carries the query string, where a filtered view keeps its state', () => {
    const ctx = buildPageContext('/insights', '?project=commcare&category=stale')
    expect(ctx.query).toEqual({ project: 'commcare', category: 'stale' })
  })

  it('omits query entirely when there is none, rather than sending an empty object', () => {
    expect(buildPageContext('/insights')).not.toHaveProperty('query')
    expect(buildPageContext('/insights', '?')).not.toHaveProperty('query')
  })

  it('still matches the route rules, which are anchored on a bare path', () => {
    // The reason search is a separate argument: `/^\/w\/([^/]+)\/?$/` stops
    // matching the moment a `?` is appended, so folding it into `path` would
    // drop every such page to the generic descriptor.
    const ctx = buildPageContext('/w/connect', '?tab=open')
    expect(ctx.surface).toBe('the project workbench')
    expect(ctx.query).toEqual({ tab: 'open' })
  })

  it('decodes what the browser encoded', () => {
    const ctx = buildPageContext('/insights', '?q=two%20words')
    expect((ctx.query as Record<string, string>).q).toBe('two words')
  })
})
