import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  onResourceChanged,
  resetResourceHandlers,
  resourceChanged,
} from './pageInvalidation'
import { describeSelection } from './pageState'

beforeEach(() => resetResourceHandlers())

describe('a page is told to re-read what it is showing', () => {
  it('calls the handler registered for that resource', () => {
    const refetch = vi.fn()
    onResourceChanged('task://', refetch)

    expect(resourceChanged('task://')).toBe(1)
    expect(refetch).toHaveBeenCalledOnce()
  })

  it('does not disturb a page showing something else', () => {
    const refetch = vi.fn()
    onResourceChanged('walkthrough://', refetch)

    resourceChanged('task://')

    expect(refetch).not.toHaveBeenCalled()
  })

  it('tells every page showing it — two tabs are two handlers', () => {
    const a = vi.fn()
    const b = vi.fn()
    onResourceChanged('task://', a)
    onResourceChanged('task://', b)

    expect(resourceChanged('task://')).toBe(2)
    expect(a).toHaveBeenCalledOnce()
    expect(b).toHaveBeenCalledOnce()
  })

  it('stops after the page unmounts', () => {
    const refetch = vi.fn()
    const stop = onResourceChanged('task://', refetch)

    stop()
    resourceChanged('task://')

    expect(refetch).not.toHaveBeenCalled()
  })

  it('is silent for a resource nobody is showing', () => {
    expect(resourceChanged('nobody://')).toBe(0)
  })
})

describe('a failing refetch cannot break the conversation', () => {
  it('survives a handler that throws', () => {
    onResourceChanged('task://', () => {
      throw new Error('the list endpoint is down')
    })

    // An exception escaping here would propagate into the socket handler and
    // could take down the connection carrying the chat — a strictly worse
    // outcome than a page showing slightly old rows.
    expect(() => resourceChanged('task://')).not.toThrow()
  })

  it('survives a handler whose promise rejects', async () => {
    onResourceChanged('task://', async () => {
      throw new Error('network')
    })

    expect(() => resourceChanged('task://')).not.toThrow()
    // And leaves no unhandled rejection behind.
    await new Promise((r) => setTimeout(r, 0))
  })

  it('still calls the other handlers when one throws', () => {
    const ok = vi.fn()
    onResourceChanged('task://', () => {
      throw new Error('boom')
    })
    onResourceChanged('task://', ok)

    resourceChanged('task://')

    expect(ok).toHaveBeenCalledOnce()
  })
})

describe('declaring the resource is what makes any of it fire', () => {
  it('carries the resource into the page state', () => {
    const state = describeSelection({
      backingTool: 'list_fleet_tasks',
      resource: 'task://',
      ids: [1, 2],
    })

    expect(state.resource).toBe('task://')
  })

  it('omits it when a page does not declare one', () => {
    // Legal, and means "describe me to the agent, but I will not refresh
    // myself" — a worse page, not a broken one.
    const state = describeSelection({ backingTool: 'list_fleet_tasks', ids: [1] })

    expect(state).not.toHaveProperty('resource')
  })
})
