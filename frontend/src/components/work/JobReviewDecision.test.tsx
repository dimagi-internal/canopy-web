// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const runTriggerNow = vi.fn<(slug: string, id: number) => Promise<unknown>>(async () => ({}))
vi.mock('@/api/triggers', () => ({ runTriggerNow: (s: string, i: number) => runTriggerNow(s, i) }))

let isAdmin = true
vi.mock('@/api/agents', () => ({ getAgent: vi.fn(async () => ({ is_admin: isAdmin })) }))

const { JobReviewDecision } = await import('./JobReviewDecision')

afterEach(() => {
  isAdmin = true
  cleanup()
  vi.clearAllMocks()
})

describe('JobReviewDecision', () => {
  it('offers Run now, not Start now, and keeps the ways to set it aside', async () => {
    const onAct = vi.fn(async () => {})
    render(<JobReviewDecision agentSlug="hal" scheduleId={7} askBody="The job failed." onAct={onAct} />)
    expect(screen.queryByText('Start now')).toBeNull()
    expect(screen.getByText('The job failed.')).toBeTruthy()
    fireEvent.click(await screen.findByText('Run now'))
    await waitFor(() => expect(runTriggerNow).toHaveBeenCalledWith('hal', 7))
    expect(await screen.findByText('The job is queued. A good run closes this on its own.')).toBeTruthy()
    fireEvent.click(screen.getByText('Decline'))
    expect(onAct).toHaveBeenCalledWith('decline')
  })

  it("shows the server's sentence when Run now is refused", async () => {
    runTriggerNow.mockRejectedValueOnce(new Error('Only an admin of hal can change a job.'))
    render(<JobReviewDecision agentSlug="hal" scheduleId={7} askBody="" onAct={vi.fn()} />)
    fireEvent.click(await screen.findByText('Run now'))
    expect(await screen.findByText('Only an admin of hal can change a job.')).toBeTruthy()
  })

  it('says the job is still running when Run now is refused for that, and not that it queued', async () => {
    runTriggerNow.mockRejectedValueOnce(new Error('This job is still running.'))
    render(<JobReviewDecision agentSlug="hal" scheduleId={7} askBody="" onAct={vi.fn()} />)
    fireEvent.click(await screen.findByText('Run now'))
    expect(await screen.findByText('This job is still running.')).toBeTruthy()
    expect(screen.queryByText(/queued/i)).toBeNull()
    // It can be pressed again once the run has finished.
    expect((screen.getByText('Run now') as HTMLButtonElement).disabled).toBe(false)
  })

  it('does not offer Run now to someone who is not an admin, and says why', async () => {
    isAdmin = false
    render(<JobReviewDecision agentSlug="hal" scheduleId={7} askBody="" onAct={vi.fn()} />)
    expect(await screen.findByText('Only an admin of hal can change a job.')).toBeTruthy()
    expect(screen.queryByText('Run now')).toBeNull()
    expect(screen.getByText('Decline')).toBeTruthy()
  })
})
