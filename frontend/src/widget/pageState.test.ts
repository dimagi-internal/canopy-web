import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  currentPageState,
  describeSelection,
  hasPageState,
  notifyPageStateChanged,
  onPageStateChanged,
  setPageStateContributor,
} from './pageState'

/** The registry is module-level (the consumer is not a React component), so
 *  each test has to put it back or the previous page leaks into the next. */
let dispose: (() => void) | null = null
beforeEach(() => {
  dispose?.()
  dispose = null
})

describe('the state channel pushes, where context was pulled', () => {
  it('tells listeners the moment the view changes', () => {
    // This is the entire reason the channel exists. The old context provider
    // was READ ONCE when a conversation opened, so a user who filtered the page
    // afterwards left the agent acting on a screen that no longer existed.
    const seen: unknown[] = []
    const stop = onPageStateChanged((s) => seen.push(s))
    let ids = [1, 2, 3]
    dispose = setPageStateContributor(() => ({ visible_ids: ids }))

    ids = [2]
    notifyPageStateChanged()

    stop()
    expect(seen.at(-1)).toEqual({ visible_ids: [2] })
  })

  it('registering is last-wins, so navigating replaces rather than merges', () => {
    const first = setPageStateContributor(() => ({ path: '/insights' }))
    dispose = setPageStateContributor(() => ({ path: '/supervisor' }))

    expect(currentPageState()).toEqual({ path: '/supervisor' })
    // The OLD page's cleanup running late must not blank the new page's state —
    // React does exactly this on a route change.
    first()
    expect(currentPageState()).toEqual({ path: '/supervisor' })
  })

  it('a contributor that throws costs only its own contribution', () => {
    dispose = setPageStateContributor(() => {
      throw new Error('page is mid-render')
    })

    // The route layer beneath it is still true, and an agent with the path and
    // no selection is far better off than an agent with nothing.
    expect(currentPageState({ path: '/insights' })).toEqual({ path: '/insights' })
  })

  it('distinguishes "no page declared anything" from "the page is empty"', () => {
    expect(hasPageState()).toBe(false)
    dispose = setPageStateContributor(() => ({}))
    expect(hasPageState()).toBe(true)
    // Without this the widget would announce a blank screen for every host that
    // simply does not use the channel, overwriting nothing with an assertion.
  })

  it('unregisters on dispose, so a page left behind stops describing itself', () => {
    const stop = setPageStateContributor(() => ({ path: '/insights' }))
    stop()

    expect(hasPageState()).toBe(false)
    expect(currentPageState()).toEqual({})
  })

  it('notifies on registration, so a page mounting mid-conversation is seen', () => {
    const listener = vi.fn()
    const stop = onPageStateChanged(listener)

    dispose = setPageStateContributor(() => ({ path: '/insights' }))

    stop()
    expect(listener).toHaveBeenCalledWith({ path: '/insights' })
  })
})

describe('describeSelection is the shape that works', () => {
  it('carries which rows and which tool resolves them', () => {
    const state = describeSelection({
      backingTool: 'list_insights',
      ids: [4471, 4472],
      filters: { category: 'stale' },
    })

    expect(state).toEqual({
      backing_tool: 'list_insights',
      visible_ids: [4471, 4472],
      visible_count: 2,
      filters: { category: 'stale' },
    })
  })

  it('carries no row data at all — that is the point', () => {
    // The agent re-reads the rows through `backing_tool`, live, with the user's
    // own permissions. A copy serialised here could go stale between render and
    // send and would be a second place an ACL could be got wrong.
    const state = describeSelection({ backingTool: 'list_insights', ids: [1, 2, 3] })

    expect(Object.keys(state).sort()).toEqual(['backing_tool', 'visible_count', 'visible_ids'])
  })

  it('omits empty filters rather than sending a hollow object', () => {
    expect(describeSelection({ backingTool: 't', ids: [], filters: {} })).not.toHaveProperty(
      'filters',
    )
  })

  it('counts what is visible, so the agent can tell a page from a whole list', () => {
    const state = describeSelection({ backingTool: 'list_insights', ids: [1, 2, 3, 4, 5] })

    expect(state.visible_count).toBe(5)
  })
})

describe('the first message must not depend on MCP being up', () => {
  /**
   * Found live: the agent replied "the canopy-web MCP server is still
   * connecting, its tools aren't loaded", so it could not call `current_page`
   * and had only `{surface, path}` — blind to the twenty rows on screen.
   *
   * MCP servers connect asynchronously and the widget creates a fresh session
   * per conversation, so the FIRST turn races that connection. The first turn
   * is the one carrying the user's question, which made the most important turn
   * depend on the flakiest link.
   */
  it('merges the selection into what rides the first message', () => {
    dispose = setPageStateContributor(() =>
      describeSelection({
        backingTool: 'list_insights',
        resource: 'insight://',
        ids: [61, 60, 59],
      }),
    )

    const forTheBlock = { ...currentPageState({ surface: 'the insights feed', path: '/insights' }) }

    // The route layer AND the selection, in one object.
    expect(forTheBlock.path).toBe('/insights')
    expect(forTheBlock.visible_ids).toEqual([61, 60, 59])
    expect(forTheBlock.backing_tool).toBe('list_insights')
  })

  it('still produces the route alone when no page declared a selection', () => {
    // A host that does not use the state channel must not end up with a block
    // that claims an empty screen.
    const forTheBlock = currentPageState({ surface: 'the supervisor inbox', path: '/supervisor' })

    expect(forTheBlock).toEqual({ surface: 'the supervisor inbox', path: '/supervisor' })
  })

  it('lets the page override a route field it knows better', () => {
    // The page is the only party that knows what it actually rendered, so its
    // own keys win over the route table's guess.
    dispose = setPageStateContributor(() => ({ surface: 'the filtered insights feed' }))

    expect(currentPageState({ surface: 'the insights feed', path: '/insights' }).surface).toBe(
      'the filtered insights feed',
    )
  })
})
