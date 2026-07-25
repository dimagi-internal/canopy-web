// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { RunnerDrillOut } from '@/api/drills'

// vi.mock is hoisted above these declarations by vitest's transform, but the
// factory below isn't *called* until '@/api/drills' is actually imported —
// which happens on the dynamic `import('./RunnerDrills')` below, well after
// these consts are assigned (same pattern as RunnerAssignments.test.tsx).
const startDrill = vi.fn<(runnerId: string, agents?: readonly string[]) => Promise<RunnerDrillOut[]>>()
const listDrills = vi.fn<(runnerId: string) => Promise<RunnerDrillOut[]>>()

vi.mock('@/api/drills', () => ({ startDrill, listDrills }))

const { RunnerDrills } = await import('./RunnerDrills')

function drill(id: number, overrides: Partial<RunnerDrillOut> = {}): RunnerDrillOut {
  return {
    id,
    agent_slug: 'echo',
    outcome: 'pass',
    summary: 'ok',
    started_at: '2026-07-24T11:00:00Z',
    finished_at: '2026-07-24T11:01:00Z',
    turn_id: null,
    ...overrides,
  }
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.useRealTimers()
})

beforeEach(() => {
  vi.setSystemTime(new Date('2026-07-24T12:00:00Z'))
})

describe('RunnerDrills', () => {
  it('renders rows with the right outcome chips', async () => {
    listDrills.mockResolvedValue([
      drill(1, { agent_slug: 'echo', outcome: 'pass' }),
      drill(2, { agent_slug: 'ada', outcome: 'fail', summary: 'boom' }),
      drill(3, { agent_slug: 'hal', outcome: 'pending', started_at: '2026-07-24T11:55:00Z', finished_at: null }),
    ])

    render(<RunnerDrills runnerId="r1" />)

    const pass = await screen.findByTestId('drill-outcome-1')
    expect(pass.textContent).toBe('pass')
    expect(pass.className).toContain('text-success')

    const fail = screen.getByTestId('drill-outcome-2')
    expect(fail.textContent).toBe('fail')
    expect(fail.className).toContain('text-destructive')

    const pending = screen.getByTestId('drill-outcome-3')
    expect(pending.textContent).toBe('pending')
    expect(pending.className).toContain('text-warning')
  })

  it('renders a stale pending row as timed out', async () => {
    // now = 12:00, started 40 minutes ago — past the 30-minute threshold.
    listDrills.mockResolvedValue([
      drill(4, { agent_slug: 'echo', outcome: 'pending', started_at: '2026-07-24T11:20:00Z', finished_at: null }),
    ])

    render(<RunnerDrills runnerId="r1" />)

    const chip = await screen.findByTestId('drill-outcome-4')
    expect(chip.textContent).toBe('timed out')
    expect(chip.className).toContain('text-destructive')
  })

  it('truncates the summary but keeps the full text in a title tooltip', async () => {
    listDrills.mockResolvedValue([drill(5, { summary: 'a very long summary that should be truncated visually' })])

    render(<RunnerDrills runnerId="r1" />)

    const row = await screen.findByTestId('drill-row-5')
    const cell = row.querySelector('[title]')
    expect(cell?.getAttribute('title')).toBe('a very long summary that should be truncated visually')
  })

  it('renders a turn id as a short hash with a tooltip when present', async () => {
    listDrills.mockResolvedValue([drill(6, { turn_id: '0123456789abcdef' })])

    render(<RunnerDrills runnerId="r1" />)

    const row = await screen.findByTestId('drill-row-6')
    const turnCell = screen.getByTitle('0123456789abcdef')
    expect(row.contains(turnCell)).toBe(true)
    expect(turnCell.textContent).toBe('01234567')
  })

  it('the drill button calls startDrill and refreshes', async () => {
    listDrills.mockResolvedValue([])
    startDrill.mockResolvedValue([drill(7)])

    render(<RunnerDrills runnerId="r1" />)
    await screen.findByText(/no drills yet/i)

    listDrills.mockResolvedValue([drill(7)])
    fireEvent.click(screen.getByTestId('drill-runner-button'))

    await waitFor(() => expect(startDrill).toHaveBeenCalledWith('r1'))
    await screen.findByTestId('drill-row-7')
  })

  it('disables the drill button while the fan-out request is in flight', async () => {
    listDrills.mockResolvedValue([])
    let resolveStart!: (v: RunnerDrillOut[]) => void
    startDrill.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveStart = resolve
        }),
    )

    render(<RunnerDrills runnerId="r1" />)
    await screen.findByText(/no drills yet/i)

    const button = screen.getByTestId('drill-runner-button') as HTMLButtonElement
    fireEvent.click(button)

    await waitFor(() => expect(button.disabled).toBe(true))

    await act(async () => {
      resolveStart([])
    })
    await waitFor(() => expect(button.disabled).toBe(false))
  })

  it('shows the error text inline when startDrill 422s (no assigned agents)', async () => {
    listDrills.mockResolvedValue([])
    startDrill.mockRejectedValue(
      new Error(
        'startDrill failed: {"type":"about:blank","title":"no assigned agents to drill — assign this runner to an agent first","status":422,"detail":"no assigned agents to drill — assign this runner to an agent first","instance":"/api/harness/runners/r1/drill"}',
      ),
    )

    render(<RunnerDrills runnerId="r1" />)
    await screen.findByText(/no drills yet/i)

    fireEvent.click(screen.getByTestId('drill-runner-button'))

    const err = await screen.findByTestId('drill-error')
    expect(err.textContent).toBe('no assigned agents to drill — assign this runner to an agent first')
    expect(err.className).toContain('text-muted-foreground')
  })

  it('polls listDrills every 10s while a row is pending, then stops once resolved', async () => {
    vi.useFakeTimers({ now: new Date('2026-07-24T12:00:00Z') })
    listDrills.mockResolvedValueOnce([
      drill(8, { outcome: 'pending', started_at: '2026-07-24T11:59:00Z', finished_at: null }),
    ])

    render(<RunnerDrills runnerId="r1" />)
    await act(async () => {
      await vi.waitFor(() => expect(listDrills).toHaveBeenCalledTimes(1))
    })

    listDrills.mockResolvedValueOnce([drill(8, { outcome: 'pass', finished_at: '2026-07-24T12:00:05Z' })])
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    expect(listDrills).toHaveBeenCalledTimes(2)

    // No more pending rows — a further 10s must NOT trigger a third poll.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    expect(listDrills).toHaveBeenCalledTimes(2)
  })

  it('polling survives transient fetch errors and continues until resolved', async () => {
    vi.useFakeTimers({ now: new Date('2026-07-24T12:00:00Z') })

    // First call: initial render — one pending drill
    listDrills.mockResolvedValueOnce([
      drill(9, { outcome: 'pending', started_at: '2026-07-24T11:59:00Z', finished_at: null }),
    ])

    render(<RunnerDrills runnerId="r1" />)
    await act(async () => {
      await vi.waitFor(() => expect(listDrills).toHaveBeenCalledTimes(1))
    })

    // Second call (10s later): poll fails with transient error
    listDrills.mockRejectedValueOnce(new Error('listDrills failed: {"detail":"network error"}'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    expect(listDrills).toHaveBeenCalledTimes(2)

    // Third call (10s later): poll succeeds and returns resolved drill
    listDrills.mockResolvedValueOnce([drill(9, { outcome: 'pass', finished_at: '2026-07-24T12:00:15Z' })])
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    expect(listDrills).toHaveBeenCalledTimes(3)

    // No more pending rows — a further 10s must NOT trigger a fourth poll.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000)
    })
    expect(listDrills).toHaveBeenCalledTimes(3)
  })
})
