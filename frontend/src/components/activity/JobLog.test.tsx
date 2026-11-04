// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

const listTurnEvents = vi.fn<(id: string) => Promise<unknown[]>>(async () => [])
vi.mock('@/api/turns', () => ({
  listTurnEvents: (id: string) => listTurnEvents(id),
  listTurnMessages: vi.fn(async () => {
    throw new Error('transcript missing')
  }),
}))

const { JobLog } = await import('./JobLog')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const ev = (seq: number, kind: string, payload: Record<string, unknown>) => ({
  seq, ts: '2026-11-04T09:00:00Z', kind, payload,
})

describe('JobLog', () => {
  it("shows the job's output as a plain log in a monospace block, in order", async () => {
    listTurnEvents.mockResolvedValueOnce([
      ev(1, 'claimed', {}),
      ev(2, 'status', { source: 'job', text: 'fetching leads' }),
      ev(3, 'status', { source: 'model', text: 'not the job' }),
      ev(4, 'status', { source: 'job', text: 'wrote 12 rows' }),
    ])
    render(<JobLog turnId="t1" resultNote="exit code 0" />)
    const log = await screen.findByTestId('job-log')
    expect(log.tagName).toBe('PRE')
    expect(log.className).toContain('font-mono')
    expect(log.textContent).toBe('fetching leads\nwrote 12 rows')
    expect(screen.getByText('exit code 0')).toBeTruthy()
  })

  it('has no chat box and never asks for a transcript', async () => {
    listTurnEvents.mockResolvedValueOnce([ev(1, 'status', { source: 'job', text: 'ok' })])
    const { container } = render(<JobLog turnId="t1" resultNote="" />)
    await screen.findByTestId('job-log')
    expect(container.querySelector('textarea, input')).toBeNull()
    expect(container.textContent?.toLowerCase()).not.toContain('transcript')
  })

  it('says so when the job printed nothing', async () => {
    listTurnEvents.mockResolvedValueOnce([ev(1, 'claimed', {})])
    render(<JobLog turnId="t1" resultNote="exit code 0" />)
    expect(await screen.findByText('The job printed nothing.')).toBeTruthy()
    expect(screen.getByText('exit code 0')).toBeTruthy()
  })
})
