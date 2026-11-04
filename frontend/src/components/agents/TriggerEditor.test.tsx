// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const createTrigger = vi.fn<(...args: unknown[]) => Promise<unknown>>(() => Promise.resolve({}))
vi.mock('@/api/triggers', () => ({
  createTrigger,
  updateTrigger: vi.fn(),
  deleteTrigger: vi.fn(),
}))
vi.mock('@/api/schedules', () => ({
  previewCron: vi.fn(() => Promise.resolve([])),
}))

const { TriggerEditor } = await import('./TriggerEditor')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('TriggerEditor agent CLI', () => {
  it('is not offered for an agent set up for one CLI, and is not sent', async () => {
    render(<TriggerEditor agentSlug="ace" agentClis={['claude-code']} trigger={null}
                           onClose={vi.fn()} onSaved={vi.fn()} />)
    expect(screen.queryByTestId('schedule-cli')).toBeNull()
    fireEvent.change(screen.getByPlaceholderText('Weekly manager report'), { target: { value: 'Post' } })
    fireEvent.change(screen.getByPlaceholderText('/echo:manager-report'), { target: { value: '/ace:turn' } })
    fireEvent.click(screen.getByText(/^Save|^Create/))
    await waitFor(() => expect(createTrigger).toHaveBeenCalled())
    expect(createTrigger.mock.calls[0][1]).not.toHaveProperty('cli')
  })

  it('saves the CLI picked', async () => {
    render(<TriggerEditor agentSlug="ace" agentClis={['claude-code', 'codex']} trigger={null}
                           onClose={vi.fn()} onSaved={vi.fn()} />)
    fireEvent.change(screen.getByPlaceholderText('Weekly manager report'), { target: { value: 'Post' } })
    fireEvent.change(screen.getByPlaceholderText('/echo:manager-report'), { target: { value: '/ace:turn' } })
    fireEvent.change(screen.getByTestId('schedule-cli'), { target: { value: 'codex' } })
    fireEvent.click(screen.getByText(/^Save|^Create/))
    await waitFor(() => expect(createTrigger).toHaveBeenCalled())
    expect(createTrigger.mock.calls[0][1]).toMatchObject({ name: 'Post', cli: 'codex' })
  })
})

describe('TriggerEditor inbound mail', () => {
  it('sends the sender binding, no cron, and the turn mode', async () => {
    render(<TriggerEditor agentSlug="hal" trigger={null} onClose={vi.fn()} onSaved={vi.fn()} />)
    fireEvent.click(screen.getByText('When mail arrives'))
    expect(screen.queryByLabelText('Cron expression')).toBeNull()
    fireEvent.change(screen.getByPlaceholderText('AWS CloudWatch alarms'), { target: { value: 'Alarms' } })
    fireEvent.change(screen.getByPlaceholderText('no-reply@sns.amazonaws.com'),
                     { target: { value: 'no-reply@sns.amazonaws.com' } })
    fireEvent.change(screen.getByLabelText('Turn mode'), { target: { value: 'manual' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(createTrigger).toHaveBeenCalled())
    const body = createTrigger.mock.calls[0][1] as Record<string, unknown>
    expect(body).toMatchObject({ kind: 'inbound_mail', name: 'Alarms',
                                 sender_address: 'no-reply@sns.amazonaws.com', turn_mode: 'manual' })
    expect(body).not.toHaveProperty('cron')
  })
})

describe('TriggerEditor plain job', () => {
  const fill = () => {
    fireEvent.change(screen.getByPlaceholderText('Weekly manager report'), { target: { value: 'Nightly sync' } })
  }

  it('offers the job choice to an agent admin, beside Run {agent}', () => {
    render(<TriggerEditor agentSlug="hal" agentName="Hal" canSetJob trigger={null} onClose={vi.fn()} onSaved={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Run Hal' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Run a job (no AI)' })).toBeTruthy()
  })

  it('does not offer it to anyone else, and the ordinary form is the same', () => {
    render(<TriggerEditor agentSlug="hal" agentName="Hal" trigger={null} onClose={vi.fn()} onSaved={vi.fn()} />)
    expect(screen.queryByRole('button', { name: 'Run a job (no AI)' })).toBeNull()
    expect(screen.getByPlaceholderText('/echo:manager-report')).toBeTruthy()
  })

  it('asks which repo and which job, says what it does, and sends job and job_repo with no prompt', async () => {
    render(<TriggerEditor agentSlug="hal" agentName="Hal" canSetJob trigger={null} onClose={vi.fn()} onSaved={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Run a job (no AI)' }))
    expect(screen.queryByPlaceholderText('/echo:manager-report')).toBeNull()
    expect(screen.getByText(
      "Runs the job of that name from the repo's .canopy/jobs.yaml on main, on a cloud machine, using Hal's connections.",
    )).toBeTruthy()
    expect(screen.getByText(
      'Jobs can reach the public internet, but not private addresses inside our network.',
    )).toBeTruthy()
    fill()
    fireEvent.change(screen.getByLabelText('Which repo'), { target: { value: 'hal-jobs' } })
    fireEvent.change(screen.getByLabelText('Which job'), { target: { value: 'sync-leads' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(createTrigger).toHaveBeenCalled())
    const body = createTrigger.mock.calls[0][1] as Record<string, unknown>
    expect(body).toMatchObject({ kind: 'cron', name: 'Nightly sync', job: 'sync-leads', job_repo: 'hal-jobs' })
    expect(body).not.toHaveProperty('prompt')
  })

  it('cannot be saved until both are named', () => {
    render(<TriggerEditor agentSlug="hal" agentName="Hal" canSetJob trigger={null} onClose={vi.fn()} onSaved={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Run a job (no AI)' }))
    fill()
    fireEvent.change(screen.getByLabelText('Which repo'), { target: { value: 'hal-jobs' } })
    expect((screen.getByText('Save') as HTMLButtonElement).disabled).toBe(true)
  })

  it("shows the server's sentence for a bad name", async () => {
    createTrigger.mockRejectedValueOnce(
      new Error("a job's name is lower-case letters, digits, '-' and '_' (up to 64), starting with a letter or digit"),
    )
    render(<TriggerEditor agentSlug="hal" agentName="Hal" canSetJob trigger={null} onClose={vi.fn()} onSaved={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Run a job (no AI)' }))
    fill()
    fireEvent.change(screen.getByLabelText('Which repo'), { target: { value: 'hal-jobs' } })
    fireEvent.change(screen.getByLabelText('Which job'), { target: { value: 'Bad Name' } })
    fireEvent.click(screen.getByText('Save'))
    expect(await screen.findByText(/a job's name is lower-case letters/)).toBeTruthy()
  })

  const existing = {
    id: 7, kind: 'cron', name: 'Nightly sync', prompt: '', job: 'sync-leads', job_repo: 'hal-jobs',
    cron: '0 9 * * *', timezone: 'UTC', cli: '', run_once_at: null,
  } as never

  it('is read-only for someone who is not an admin, with the sentence', () => {
    render(<TriggerEditor agentSlug="hal" agentName="Hal" trigger={existing} onClose={vi.fn()} onSaved={vi.fn()} />)
    expect(screen.getByText('Only an admin of hal can change a job.')).toBeTruthy()
    expect(screen.getByText('sync-leads')).toBeTruthy()
    expect(screen.getByText('hal-jobs')).toBeTruthy()
    expect(screen.queryByText('Save')).toBeNull()
    expect(screen.queryByLabelText('Which job')).toBeNull()
  })

  it('opens an existing job for an admin with its repo and job filled in', () => {
    render(<TriggerEditor agentSlug="hal" agentName="Hal" canSetJob trigger={existing} onClose={vi.fn()} onSaved={vi.fn()} />)
    expect((screen.getByLabelText('Which repo') as HTMLInputElement).value).toBe('hal-jobs')
    expect((screen.getByLabelText('Which job') as HTMLInputElement).value).toBe('sync-leads')
  })
})
