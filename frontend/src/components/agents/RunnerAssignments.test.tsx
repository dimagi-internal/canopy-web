// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { AgentRunnerOut } from '@/api/agents'
import type { RunnerOut } from '@/api/harness'

// vi.mock is hoisted above these declarations by vitest's transform, but the
// factory below isn't *called* until '@/api/agents' / '@/api/harness' are
// actually imported — which happens on the dynamic `import('./RunnerAssignments')`
// at the bottom of this setup block, well after these consts are assigned.
const getAgentRunners = vi.fn<() => Promise<AgentRunnerOut[]>>()
const putAgentRunners =
  vi.fn<(slug: string, rows: readonly { runnerId: string; enabled: boolean }[]) => Promise<AgentRunnerOut[]>>()
const listRunners = vi.fn<() => Promise<RunnerOut[]>>()

vi.mock('@/api/agents', () => ({ getAgentRunners, putAgentRunners }))
vi.mock('@/api/harness', () => ({ listRunners }))

const { RunnerAssignments } = await import('./RunnerAssignments')

function runner(id: string, overrides: Partial<AgentRunnerOut> = {}): AgentRunnerOut {
  return {
    runner_id: id,
    runner_name: `Runner ${id}`,
    kind: 'emdash',
    rank: 1,
    online: true,
    ready: true,
    enabled: true,
    ...overrides,
  }
}

function fleetRunner(id: string, overrides: Partial<RunnerOut> = {}): RunnerOut {
  return {
    id,
    name: `Runner ${id}`,
    kind: 'emdash',
    status: 'online',
    status_note: '',
    ready: true,
    ready_note: '',
    last_heartbeat_at: null,
    capabilities: {},
    host: 'host',
    code_branch: 'main',
    workspace: null,
    paired_by_email: null,
    ...overrides,
  }
}

// A promise plus its resolve/reject, so a test can control exactly when a
// putAgentRunners call settles — needed to reproduce the out-of-order-resolve race.
function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RunnerAssignments', () => {
  it('renders chips in rank order', async () => {
    getAgentRunners.mockResolvedValue([runner('a', { rank: 1 }), runner('b', { rank: 2 })])
    listRunners.mockResolvedValue([fleetRunner('a'), fleetRunner('b')])

    render(<RunnerAssignments agentSlug="echo" />)

    await screen.findByTestId('runner-chip-a')
    const chips = screen.getAllByTestId(/^runner-chip-/)
    expect(chips.map((c) => c.getAttribute('data-testid'))).toEqual(['runner-chip-a', 'runner-chip-b'])
  })

  it('shows the unroutable empty state when no runners are assigned', async () => {
    getAgentRunners.mockResolvedValue([])
    listRunners.mockResolvedValue([fleetRunner('a')])

    render(<RunnerAssignments agentSlug="echo" />)

    expect(await screen.findByText(/unroutable/i)).toBeTruthy()
  })

  it('moving a runner down PUTs the swapped row order', async () => {
    getAgentRunners.mockResolvedValue([runner('a', { rank: 1 }), runner('b', { rank: 2 })])
    listRunners.mockResolvedValue([fleetRunner('a'), fleetRunner('b')])
    putAgentRunners.mockResolvedValue([runner('b', { rank: 1 }), runner('a', { rank: 2 })])

    render(<RunnerAssignments agentSlug="echo" />)
    await screen.findByTestId('runner-chip-a')

    fireEvent.click(screen.getByLabelText('Move Runner a down'))

    await waitFor(() =>
      expect(putAgentRunners).toHaveBeenCalledWith('echo', [
        { runnerId: 'b', enabled: true },
        { runnerId: 'a', enabled: true },
      ]),
    )
  })

  it('toggling a runner off PUTs it disabled, keeps it in the list, and greys it out', async () => {
    getAgentRunners.mockResolvedValue([runner('a', { rank: 1 }), runner('b', { rank: 2 })])
    listRunners.mockResolvedValue([fleetRunner('a'), fleetRunner('b')])
    putAgentRunners.mockResolvedValue([runner('a', { rank: 1, enabled: false }), runner('b', { rank: 2 })])

    render(<RunnerAssignments agentSlug="echo" />)
    await screen.findByTestId('runner-chip-a')

    fireEvent.click(screen.getByLabelText('Disable Runner a'))

    await waitFor(() =>
      expect(putAgentRunners).toHaveBeenCalledWith('echo', [
        { runnerId: 'a', enabled: false },
        { runnerId: 'b', enabled: true },
      ]),
    )
    // Still in the list — no removal — just visibly disabled.
    const chip = await screen.findByTestId('runner-chip-a')
    expect(chip.className).toMatch(/opacity-50/)
    expect(await screen.findByLabelText('Enable Runner a')).toBeTruthy()
  })

  it('toggling a disabled runner back on PUTs it enabled', async () => {
    getAgentRunners.mockResolvedValue([runner('a', { rank: 1, enabled: false }), runner('b', { rank: 2 })])
    listRunners.mockResolvedValue([fleetRunner('a'), fleetRunner('b')])
    putAgentRunners.mockResolvedValue([runner('a', { rank: 1 }), runner('b', { rank: 2 })])

    render(<RunnerAssignments agentSlug="echo" />)
    await screen.findByTestId('runner-chip-a')

    fireEvent.click(screen.getByLabelText('Enable Runner a'))

    await waitFor(() =>
      expect(putAgentRunners).toHaveBeenCalledWith('echo', [
        { runnerId: 'a', enabled: true },
        { runnerId: 'b', enabled: true },
      ]),
    )
    await waitFor(() => expect(screen.getByTestId('runner-chip-a').className).not.toMatch(/opacity-50/))
  })

  it('reverts optimistic state and shows an error when the PUT fails', async () => {
    getAgentRunners.mockResolvedValue([runner('a', { rank: 1 }), runner('b', { rank: 2 })])
    listRunners.mockResolvedValue([fleetRunner('a'), fleetRunner('b')])
    putAgentRunners.mockRejectedValue(new Error('boom'))

    render(<RunnerAssignments agentSlug="echo" />)
    await screen.findByTestId('runner-chip-a')

    fireEvent.click(screen.getByLabelText('Disable Runner a'))

    // Optimistic disable happens immediately...
    expect(screen.getByTestId('runner-chip-a').className).toMatch(/opacity-50/)

    // ...then reverts once the PUT rejects, and surfaces the error.
    await waitFor(() => expect(screen.getByTestId('runner-chip-a').className).not.toMatch(/opacity-50/))
    expect(await screen.findByText('boom')).toBeTruthy()
  })

  it('serializes overlapping commits: an older PUT resolving after a newer one is ignored', async () => {
    getAgentRunners.mockResolvedValue([
      runner('a', { rank: 1 }),
      runner('b', { rank: 2 }),
      runner('c', { rank: 3 }),
    ])
    listRunners.mockResolvedValue([fleetRunner('a'), fleetRunner('b'), fleetRunner('c')])

    const first = deferred<AgentRunnerOut[]>()
    const second = deferred<AgentRunnerOut[]>()
    putAgentRunners
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)

    render(<RunnerAssignments agentSlug="echo" />)
    await screen.findByTestId('runner-chip-a')

    // First click: move `a` down -> [b, a, c]. Still in flight.
    fireEvent.click(screen.getByLabelText('Move Runner a down'))
    await waitFor(() => expect(putAgentRunners).toHaveBeenCalledTimes(1))
    expect(putAgentRunners).toHaveBeenNthCalledWith(1, 'echo', [
      { runnerId: 'b', enabled: true },
      { runnerId: 'a', enabled: true },
      { runnerId: 'c', enabled: true },
    ])

    // Second click, fired before the first resolves: disable `c` on top of the
    // OPTIMISTIC (post-first-click) order -> [b, a, c-disabled]. Proves the
    // mutation reads current state, not the stale render-time closure. `c`
    // stays in the list — there is no removal affordance anymore.
    fireEvent.click(screen.getByLabelText('Disable Runner c'))
    await waitFor(() => expect(putAgentRunners).toHaveBeenCalledTimes(2))
    expect(putAgentRunners).toHaveBeenNthCalledWith(2, 'echo', [
      { runnerId: 'b', enabled: true },
      { runnerId: 'a', enabled: true },
      { runnerId: 'c', enabled: false },
    ])

    // Resolve the NEWER (second) commit first, then the OLDER (first) commit —
    // the out-of-order-resolve case. The newer result must win.
    await act(async () => {
      second.resolve([runner('b', { rank: 1 }), runner('a', { rank: 2 }), runner('c', { rank: 3, enabled: false })])
    })
    await waitFor(() => expect(screen.getByTestId('runner-chip-c').className).toMatch(/opacity-50/))

    await act(async () => {
      first.resolve([runner('b', { rank: 1 }), runner('a', { rank: 2 }), runner('c', { rank: 3 })])
    })

    // The older response landing late (still `enabled: true`) must NOT
    // resurrect `c` back to enabled.
    expect(screen.getByTestId('runner-chip-c').className).toMatch(/opacity-50/)
    const chips = screen.getAllByTestId(/^runner-chip-/)
    expect(chips.map((c) => c.getAttribute('data-testid'))).toEqual([
      'runner-chip-b',
      'runner-chip-a',
      'runner-chip-c',
    ])
  })
})
