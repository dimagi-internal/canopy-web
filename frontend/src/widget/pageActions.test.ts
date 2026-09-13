import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  UnknownPageActionError,
  currentSpecs,
  onPageActionsChanged,
  registerPageAction,
  runPageAction,
} from './pageActions'

/**
 * The page's side of the action registry. The failure worth guarding is an
 * action that looks callable but is not — the agent then reports doing
 * something that never happened.
 */

const disposers: Array<() => void> = []
afterEach(() => {
  while (disposers.length) disposers.pop()!()
})

function register(name: string, run = vi.fn(), opts = {}) {
  const off = registerPageAction({ name, ...opts }, run)
  disposers.push(off)
  return { run, off }
}

describe('declaring', () => {
  it('carries the schema, which is the point', () => {
    register('dismissInsights', vi.fn(), {
      description: 'Dismiss things',
      parameters: { type: 'object', properties: { ids: { type: 'array' } }, required: ['ids'] },
    })
    const [spec] = currentSpecs()
    expect(spec.description).toBe('Dismiss things')
    expect(spec.parameters).toMatchObject({ required: ['ids'] })
  })

  it('sorts, so re-render order does not change the tool list', () => {
    register('zed')
    register('alpha')
    expect(currentSpecs().map((s) => s.name)).toEqual(['alpha', 'zed'])
  })

  it('notifies subscribers so the widget can re-declare as the page moves', () => {
    const seen: string[][] = []
    const off = onPageActionsChanged((s) => seen.push(s.map((x) => x.name)))
    disposers.push(off)
    register('a')
    expect(seen.at(-1)).toEqual(['a'])
  })
})

describe('running', () => {
  it('passes args and returns the result', async () => {
    const run = vi.fn().mockResolvedValue({ dismissed: 2 })
    register('act', run)
    await expect(runPageAction('act', { ids: [1, 2] })).resolves.toEqual({ dismissed: 2 })
    expect(run).toHaveBeenCalledWith({ ids: [1, 2] })
  })

  it('rejects an unknown action rather than doing nothing', async () => {
    // A silent no-op is indistinguishable to the agent from success.
    await expect(runPageAction('nope', {})).rejects.toBeInstanceOf(UnknownPageActionError)
  })

  it('propagates a refusal as an error the agent can read', async () => {
    register('act', vi.fn().mockRejectedValue(new Error('this list has moved on')))
    await expect(runPageAction('act', {})).rejects.toThrow('this list has moved on')
  })
})

describe('withdrawing', () => {
  it('an unregistered action is no longer callable', async () => {
    const { off } = register('act')
    off()
    disposers.length = 0
    await expect(runPageAction('act', {})).rejects.toBeInstanceOf(UnknownPageActionError)
  })

  it('a superseded disposer does not withdraw the live action', async () => {
    // React can fire an old component's cleanup AFTER a new one registered the
    // same name; deleting unconditionally would withdraw a live action and the
    // agent would be told it does not exist.
    const first = vi.fn()
    const second = vi.fn().mockResolvedValue('second')
    const offFirst = registerPageAction({ name: 'act' }, first)
    const offSecond = registerPageAction({ name: 'act' }, second)
    disposers.push(offSecond)

    offFirst()

    await expect(runPageAction('act', {})).resolves.toBe('second')
    expect(first).not.toHaveBeenCalled()
  })
})
