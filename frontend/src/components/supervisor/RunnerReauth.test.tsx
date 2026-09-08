// @vitest-environment jsdom
//
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { RunnerReauth } from './RunnerReauth'
import type { RunnerMint } from '@/api/harness'

// Signing a cloud runner back in from a browser. The screen a human sees is
// deliberately two steps — open a link, paste a code — so these tests are about
// which of those is on screen at each stage, and about the one thing that must
// never appear: a secret.

const api = vi.hoisted(() => ({
  getRunnerMint: vi.fn(),
  startRunnerMint: vi.fn(),
  submitRunnerMintCode: vi.fn(),
}))

vi.mock('@/api/harness', () => api)

const URL_ = 'https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=y'

function mint(over: Partial<RunnerMint> = {}): RunnerMint {
  return {
    id: 'm1', status: 'requested', authorize_url: '', detail: '',
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    ...over,
  } as RunnerMint
}

beforeEach(() => {
  vi.clearAllMocks()
  api.getRunnerMint.mockResolvedValue(null)
})
afterEach(cleanup)

/** Establish PRESENCE: an outcome is only shown to an operator who started the
 *  sign-in in this visit, so every test that asserts on one has to click first.
 *  That is the rule under test, not incidental setup. */
async function startHere() {
  api.startRunnerMint.mockResolvedValue(mint({ status: 'requested' }))
  // Flush the initial load by draining microtasks rather than waitFor: one of
  // these tests runs under fake timers, where waitFor can never advance and
  // would hang the whole file.
  await act(async () => { await Promise.resolve() })
  await act(async () => { fireEvent.click(screen.getByTestId('reauth-start')) })
}

describe('RunnerReauth', () => {
  it('offers a single button when nothing is in flight', async () => {
    render(<RunnerReauth runnerId="r1" />)
    await waitFor(() => expect(screen.getByTestId('reauth-start')).toBeTruthy())
    expect(screen.queryByTestId('reauth-awaiting-code')).toBeNull()
  })

  it('shows the link and the code box once the runner has a URL', async () => {
    api.getRunnerMint.mockResolvedValue(mint({ status: 'awaiting_code', authorize_url: URL_ }))
    render(<RunnerReauth runnerId="r1" />)
    await waitFor(() => expect(screen.getByTestId('reauth-url')).toBeTruthy())
    expect(screen.getByTestId('reauth-url').getAttribute('href')).toBe(URL_)
    // Opened in a new tab: losing the supervisor page mid-sign-in would strand
    // the mint with nowhere to paste the code back.
    expect(screen.getByTestId('reauth-url').getAttribute('target')).toBe('_blank')
    expect(screen.getByTestId('reauth-code')).toBeTruthy()
  })

  it('will not submit an empty code', async () => {
    api.getRunnerMint.mockResolvedValue(mint({ status: 'awaiting_code', authorize_url: URL_ }))
    render(<RunnerReauth runnerId="r1" />)
    await waitFor(() => expect(screen.getByTestId('reauth-submit')).toBeTruthy())
    expect(screen.getByTestId('reauth-submit').hasAttribute('disabled')).toBe(true)
  })

  it('sends the pasted code and clears it from the box', async () => {
    api.getRunnerMint.mockResolvedValue(mint({ status: 'awaiting_code', authorize_url: URL_ }))
    api.submitRunnerMintCode.mockResolvedValue(mint({ status: 'completing' }))
    render(<RunnerReauth runnerId="r1" />)
    await waitFor(() => expect(screen.getByTestId('reauth-code')).toBeTruthy())

    fireEvent.change(screen.getByTestId('reauth-code'), { target: { value: 'the-code' } })
    await act(async () => { fireEvent.click(screen.getByTestId('reauth-submit')) })

    expect(api.submitRunnerMintCode).toHaveBeenCalledWith('r1', 'the-code')
    // The code is single-use. Leaving it in the box invites a second submit that
    // can only fail, and leaves a spent secret on screen.
    await waitFor(() => expect(screen.queryByTestId('reauth-code')).toBeNull())
  })

  it('says so when a runner never picks the request up', async () => {
    // The common real failure: this is a box we already suspect is unhealthy, so
    // spinning forever hides the one thing the operator needs to know.
    vi.useFakeTimers()
    try {
      api.getRunnerMint.mockResolvedValue(mint({ status: 'requested' }))
      render(<RunnerReauth runnerId="r1" />)
      await act(async () => { await Promise.resolve() })
      await act(async () => { vi.advanceTimersByTime(95_000) })
      expect(screen.getByTestId('reauth-stalled')).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps polling after the wait limit, so a late failure still lands', async () => {
    // The bug this pins: the poll callback hit `return` before refresh() once the
    // wait limit passed, so after 90s the page stopped looking. A mint that
    // failed four minutes in was never fetched, and the screen sat on
    // "Finishing the sign-in on the runner…" indefinitely while the server had
    // said `failed` the whole time. Reported 2026-09-08.
    vi.useFakeTimers()
    try {
      api.getRunnerMint.mockResolvedValue(mint({ status: 'requested' }))
      render(<RunnerReauth runnerId="r1" />)
      await startHere()
      api.getRunnerMint.mockResolvedValue(mint({ status: 'completing' }))

      await act(async () => { vi.advanceTimersByTime(95_000) })
      const callsAfterLimit = api.getRunnerMint.mock.calls.length

      api.getRunnerMint.mockResolvedValue(
        mint({ status: 'failed', detail: 'the code was rejected' }))
      await act(async () => { vi.advanceTimersByTime(30_000) })

      expect(api.getRunnerMint.mock.calls.length).toBeGreaterThan(callsAfterLimit)
      expect(screen.getByTestId('reauth-failed').textContent).toContain('the code was rejected')
    } finally {
      vi.useRealTimers()
    }
  })

  it('reports a failed sign-in with the runner’s reason', async () => {
    api.getRunnerMint.mockResolvedValue(mint({ status: 'requested' }))
    render(<RunnerReauth runnerId="r1" />)
    await startHere()
    api.getRunnerMint.mockResolvedValue(
      mint({ status: 'failed', detail: 'the code had expired' }))
    // Arrives on the poll tick (POLL_MS = 3s), not synchronously.
    await waitFor(() => expect(screen.getByTestId('reauth-failed').textContent)
      .toContain('the code had expired'), { timeout: 4000 })
    // and offers a retry rather than a dead end
    expect(screen.getByTestId('reauth-start')).toBeTruthy()
  })

  it('does not greet you with the outcome of an attempt nobody remembers', async () => {
    // Observed 2026-09-08: a mint abandoned at 14:39 was still showing "This
    // sign-in expired before it was finished" in red, on a fresh page load an
    // hour later, above a perfectly healthy box. Opening the page cold should
    // show the CURRENT state and a way in — not a verdict on someone else's
    // attempt. Age is irrelevant; presence is the test.
    api.getRunnerMint.mockResolvedValue(mint({
      status: 'failed',
      detail: 'This sign-in expired before it was finished',
    }))
    render(<RunnerReauth runnerId="r1" />)
    await waitFor(() => expect(screen.getByTestId('reauth-start')).toBeTruthy())
    expect(screen.queryByTestId('reauth-failed')).toBeNull()
    // …and it offers a plain start, not "again" for something they never did.
    expect(screen.getByTestId('reauth-start').textContent).toContain('Start sign-in')
  })

  it('still shows an outcome you were actually present for', async () => {
    // The diagnostic path: you clicked, it failed, you get to read why. This is
    // what the whole runner-side error reporting exists to deliver, so the
    // suppression above must not swallow it.
    api.getRunnerMint.mockResolvedValue(mint({ status: 'requested' }))
    render(<RunnerReauth runnerId="r1" />)
    await startHere()
    api.getRunnerMint.mockResolvedValue(
      mint({ status: 'failed', detail: 'the code was rejected' }))
    await waitFor(() => expect(screen.getByTestId('reauth-failed').textContent)
      .toContain('the code was rejected'), { timeout: 4000 })
  })

  it('never renders a token', async () => {
    api.getRunnerMint.mockResolvedValue(mint({ status: 'requested' }))
    const { container } = render(<RunnerReauth runnerId="r1" />)
    await startHere()
    api.getRunnerMint.mockResolvedValue(mint({ status: 'done', detail: 'signed in' }))
    await waitFor(() => expect(screen.getByTestId('reauth-done')).toBeTruthy(),
      { timeout: 4000 })
    expect(container.textContent).not.toContain('sk-ant-')
  })
})
