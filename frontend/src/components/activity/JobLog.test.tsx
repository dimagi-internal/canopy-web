// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'

type Ev = { seq: number; ts: string; kind: string; payload: Record<string, unknown> }
const listTurnEvents = vi.fn<(id: string, after?: number) => Promise<Ev[]>>(async () => [])
vi.mock('@/api/turns', () => ({
  listTurnEvents: (id: string, after?: number) => listTurnEvents(id, after),
  listTurnMessages: vi.fn(async () => {
    throw new Error('transcript missing')
  }),
}))
const getTurn = vi.fn<(id: string) => Promise<{ status: string; result_note: string }>>()
vi.mock('@/api/harness', () => ({ getTurn: (id: string) => getTurn(id) }))

const { JobLog, JOB_LOG_VIEW_CHARS } = await import('./JobLog')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.useRealTimers()
})

const ev = (seq: number, kind: string, payload: Record<string, unknown>): Ev => ({
  seq, ts: '2026-11-04T09:00:00Z', kind, payload,
})
const out = (seq: number, text: string) => ev(seq, 'status', { source: 'job', text })

/** The server: everything past `after`, at most 500 at a time. */
function serve(events: Ev[]) {
  listTurnEvents.mockImplementation(async (_id, after = 0) =>
    events.filter((e) => e.seq > after).slice(0, 500),
  )
}

describe('JobLog', () => {
  it("shows the job's output as a plain log in a monospace block, joined as the box sent it", async () => {
    serve([
      ev(1, 'claimed', {}),
      out(2, 'fetching leads\nwrote 1'),
      ev(3, 'status', { source: 'model', text: 'not the job' }),
      out(4, '2 rows\n'),
    ])
    render(<JobLog turnId="t1" resultNote="exit code 0" status="done" />)
    const log = await screen.findByTestId('job-log')
    expect(log.tagName).toBe('PRE')
    expect(log.className).toContain('font-mono')
    // No line break is invented where one batch ended and the next began.
    expect(log.textContent).toBe('fetching leads\nwrote 12 rows\n')
    expect(screen.getByText('exit code 0')).toBeTruthy()
  })

  it('has no chat box and never asks for a transcript', async () => {
    serve([out(1, 'ok')])
    const { container } = render(<JobLog turnId="t1" resultNote="" status="done" />)
    await screen.findByTestId('job-log')
    expect(container.querySelector('textarea, input')).toBeNull()
    expect(container.textContent?.toLowerCase()).not.toContain('transcript')
  })

  it('says so when the job printed nothing', async () => {
    serve([ev(1, 'claimed', {})])
    render(<JobLog turnId="t1" resultNote="exit code 0" status="done" />)
    expect(await screen.findByText('The job printed nothing.')).toBeTruthy()
    expect(screen.getByText('exit code 0')).toBeTruthy()
  })

  it('reads every page of a log longer than one page of events', async () => {
    const events = Array.from({ length: 1234 }, (_, i) => out(i + 1, `${i}\n`))
    serve(events)
    render(<JobLog turnId="t1" resultNote="" status="done" />)
    const log = await screen.findByTestId('job-log')
    expect(log.textContent).toBe(events.map((e) => e.payload.text).join(''))
    expect(listTurnEvents.mock.calls.map((c) => c[1])).toEqual([0, 500, 1000, 1234])
    expect(screen.queryByText(/earlier output was cut/i)).toBeNull()
  })

  it('keeps the last 1 MB in view and says in one line that earlier output was cut', async () => {
    const chunk = 'x'.repeat(128 * 1024 - 1) + '\n'
    const events = Array.from({ length: 10 }, (_, i) => out(i + 1, chunk))
    serve([...events, out(11, 'the end\n')])
    render(<JobLog turnId="t1" resultNote="" status="done" />)
    const log = await screen.findByTestId('job-log')
    expect(log.textContent?.length).toBe(JOB_LOG_VIEW_CHARS)
    expect(JOB_LOG_VIEW_CHARS).toBe(1024 * 1024)
    expect(log.textContent?.endsWith('the end\n')).toBe(true)
    expect(screen.getAllByText('Earlier output was cut: this shows the last 1 MB.')).toHaveLength(1)
  })

  it('refreshes while the job runs, reading only what is new, and stops when it ends', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const events = [out(1, 'one\n')]
    serve(events)
    getTurn.mockResolvedValue({ status: 'running', result_note: '' })
    render(<JobLog turnId="t1" resultNote="" status="running" />)
    await waitFor(() => expect(screen.getByTestId('job-log').textContent).toBe('one\n'))

    events.push(out(2, 'two\n'))
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    await waitFor(() => expect(screen.getByTestId('job-log').textContent).toBe('one\ntwo\n'))
    // Never from the start again: a long log costs the new lines only.
    expect(listTurnEvents.mock.calls.map((c) => c[1])).not.toContain(undefined)
    expect(listTurnEvents.mock.calls.filter((c) => c[1] === 0)).toHaveLength(1)

    events.push(out(3, 'three\n'))
    getTurn.mockResolvedValue({ status: 'failed', result_note: 'exit code 3' })
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    // What it printed last and how it ended both arrive; then nothing more is asked.
    await waitFor(() => expect(screen.getByTestId('job-log').textContent).toBe('one\ntwo\nthree\n'))
    expect(screen.getByText('exit code 3')).toBeTruthy()
    const asked = listTurnEvents.mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(30000) })
    expect(listTurnEvents.mock.calls.length).toBe(asked)
  })

  it('does not say a running job printed nothing', async () => {
    serve([ev(1, 'claimed', {})])
    getTurn.mockResolvedValue({ status: 'running', result_note: '' })
    render(<JobLog turnId="t1" resultNote="" status="running" />)
    expect(await screen.findByText('Nothing printed yet.')).toBeTruthy()
    expect(screen.queryByText('The job printed nothing.')).toBeNull()
  })

  it('keeps what it has when a refresh fails', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    serve([out(1, 'one\n')])
    getTurn.mockResolvedValue({ status: 'running', result_note: '' })
    render(<JobLog turnId="t1" resultNote="" status="running" />)
    await waitFor(() => expect(screen.getByTestId('job-log').textContent).toBe('one\n'))
    getTurn.mockRejectedValueOnce(new Error('offline'))
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(screen.getByTestId('job-log').textContent).toBe('one\n')
  })
})
