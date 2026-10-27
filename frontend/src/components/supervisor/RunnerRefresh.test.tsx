// @vitest-environment jsdom
//
// A runner's Refresh: findable, and it says where it is. The bar is the evening
// of 2026-10-27 on cloud-ec2-2-marshellis: a small grey button in the Health
// card, then a disabled "Refresh requested" and no hint of when it would run,
// why it was waiting, or whether it had finished.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { RunnerOut } from '@/api/harness'

const refreshRunner = vi.fn<(id: string) => Promise<RunnerOut>>()
vi.mock('@/api/harness', () => ({ refreshRunner }))

const { RunnerRefreshButton, RunnerRefreshStatus } = await import('./RunnerRefresh')

const NOW = new Date('2026-10-27T20:16:19Z')
const ASKED = '2026-10-27T20:13:19Z'

function runner(overrides: Partial<RunnerOut> = {}): RunnerOut {
  return {
    id: 'r1', name: 'cloud-ec2-2', kind: 'cloud', status: 'online', can_administer: true,
    refresh_pending: false, refresh: { state: 'none', waiting_on: [], in_flight: 0, changes: [], problems: [] },
    ...overrides,
  } as RunnerOut
}

function pending(refresh: Partial<NonNullable<RunnerOut['refresh']>>): RunnerOut {
  return runner({
    refresh_pending: true,
    refresh: { state: 'pending', requested_at: ASKED, requested_by: 'Jonathan Jackson', waiting_on: [],
      in_flight: 0, changes: [], problems: [], ...refresh },
  })
}

afterEach(() => {
  cleanup()
  refreshRunner.mockReset()
})

describe('the Refresh button', () => {
  it('is a labelled primary action that says what it updates', () => {
    render(<RunnerRefreshButton runner={runner()} />)
    expect(screen.getByTestId('runner-refresh').textContent).toBe('Refresh runner — update plugins, canopy CLI, Claude Code')
  })

  it('on a laptop names only what a laptop refresh updates', () => {
    render(<RunnerRefreshButton runner={runner({ kind: 'emdash' })} />)
    expect(screen.getByTestId('runner-refresh').textContent).toBe('Refresh runner — update plugins, canopy CLI')
  })

  it('asks the server and hands back the fresh row', async () => {
    const onChanged = vi.fn()
    refreshRunner.mockResolvedValue(pending({}))
    render(<RunnerRefreshButton runner={runner()} onChanged={onChanged} />)
    fireEvent.click(screen.getByTestId('runner-refresh'))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(refreshRunner).toHaveBeenCalledWith('r1')
  })

  it('is disabled while one is pending', () => {
    render(<RunnerRefreshButton runner={pending({})} />)
    const b = screen.getByTestId('runner-refresh') as HTMLButtonElement
    expect(b.disabled).toBe(true)
    expect(b.textContent).toBe('Refresh pending…')
  })

  it("stays available when only canopy's own CLI-behind ask is pending", () => {
    render(<RunnerRefreshButton runner={runner({ refresh_pending: true })} />)
    expect((screen.getByTestId('runner-refresh') as HTMLButtonElement).disabled).toBe(false)
  })

  it('is absent for someone who may not administer the box', () => {
    render(<RunnerRefreshButton runner={runner({ can_administer: false })} />)
    expect(screen.queryByTestId('runner-refresh')).toBeNull()
  })

  it('says so when the request fails', async () => {
    refreshRunner.mockRejectedValue(new Error('refreshRunner failed: 404'))
    render(<RunnerRefreshButton runner={runner()} />)
    fireEvent.click(screen.getByTestId('runner-refresh'))
    await waitFor(() => expect(screen.getByTestId('runner-refresh-error').textContent).toContain('404'))
  })
})

describe('the Refresh status', () => {
  it('shows nothing when no one asked', () => {
    const { container } = render(<RunnerRefreshStatus runner={runner()} now={NOW} />)
    expect(container.innerHTML).toBe('')
  })

  it('pending: who asked, how long ago, and that they will be told', () => {
    render(<RunnerRefreshStatus runner={pending({})} now={NOW} />)
    const panel = screen.getByTestId('runner-refresh-pending')
    expect(panel.textContent).toContain('Refresh requested 3 min ago by Jonathan Jackson — pending')
    expect(screen.getByTestId('runner-refresh-why').textContent).toContain('Nothing is in the way')
    expect(panel.textContent).toContain('gets an email and a push when it finishes')
  })

  it('pending while busy: counts the turns it waits on', () => {
    render(<RunnerRefreshStatus runner={pending({ waiting_on: ['busy'], in_flight: 2 })} now={NOW} />)
    expect(screen.getByTestId('runner-refresh-why').textContent).toBe(
      'Waiting for 2 turns in flight to finish — a cloud box restarts only when idle.')
  })

  it('pending behind its floor: says when it can run', () => {
    render(<RunnerRefreshStatus runner={pending({ waiting_on: ['floor'], eligible_at: '2026-10-27T20:28:19Z' })} now={NOW} />)
    expect(screen.getByTestId('runner-refresh-why').textContent).toContain('it can run in 12 min')
  })

  it('pending on a dark box: says it is offline', () => {
    render(<RunnerRefreshStatus runner={pending({ waiting_on: ['offline'] })} now={NOW} />)
    expect(screen.getByTestId('runner-refresh-why').textContent).toContain('It is offline')
  })

  it('stuck past 30 minutes: says so and where it went', () => {
    render(<RunnerRefreshStatus runner={pending({})} now={new Date('2026-10-27T20:50:00Z')} />)
    const panel = screen.getByTestId('runner-refresh-pending')
    expect(panel.textContent).toContain('still waiting')
    expect(panel.textContent).toContain('it is on the Alerts list')
  })

  it('done: when, how long, and the before → after of what changed', () => {
    render(<RunnerRefreshStatus now={NOW} runner={runner({
      refresh: {
        state: 'done', requested_at: '2026-10-27T20:08:19Z', requested_by: 'Jonathan Jackson',
        finished_at: '2026-10-27T20:14:19Z', took_seconds: 360, waiting_on: [], in_flight: 0, problems: [],
        changes: [
          { name: 'Claude Code', before: '2.1.1', after: '2.1.5' },
          { name: 'canopy CLI', before: '0.2.500', after: '0.2.509' },
        ],
      },
    })} />)
    const panel = screen.getByTestId('runner-refresh-done')
    expect(panel.textContent).toContain('Refreshed 2 min ago (took 6 min)')
    expect(screen.getByTestId('runner-refresh-change-Claude Code').textContent).toBe('Claude Code2.1.1→2.1.5')
    expect(screen.getByTestId('runner-refresh-change-canopy CLI').textContent).toBe('canopy CLI0.2.500→0.2.509')
  })

  it('done with problems: names them', () => {
    render(<RunnerRefreshStatus now={NOW} runner={runner({
      refresh: {
        state: 'done', requested_at: ASKED, requested_by: '', finished_at: '2026-10-27T20:15:19Z',
        took_seconds: 120, waiting_on: [], in_flight: 0, changes: [], problems: ['bootstrap: line 9: boom'],
      },
    })} />)
    expect(screen.getByTestId('runner-refresh-done').textContent).toContain('with problems')
    expect(screen.getByTestId('runner-refresh-done').textContent).toContain('nothing it reports changed version')
    expect(screen.getByTestId('runner-refresh-problem').textContent).toBe('bootstrap: line 9: boom')
  })
})
