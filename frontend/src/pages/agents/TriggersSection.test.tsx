// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const listTriggers = vi.fn<(slug: string) => Promise<unknown[]>>(async () => [])
vi.mock('@/api/triggers', () => ({
  listTriggers: (slug: string) => listTriggers(slug),
  runTriggerNow: vi.fn(),
  updateTrigger: vi.fn(),
  createTrigger: vi.fn(),
  deleteTrigger: vi.fn(),
}))
vi.mock('@/api/schedules', () => ({ previewCron: vi.fn(async () => []) }))

let isAdmin = true
vi.mock('react-router-dom', async (orig) => ({
  ...(await orig<typeof import('react-router-dom')>()),
  useOutletContext: () => ({
    agent: { slug: 'hal', name: 'Hal', clis: [], is_admin: isAdmin },
    canEdit: true,
  }),
}))

const { TriggersSection } = await import('./TriggersSection')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  isAdmin = true
})

function row(over: Record<string, unknown> = {}) {
  return {
    id: 1, kind: 'cron', name: 'Nightly sync', prompt: '', job: '', job_repo: '', cron: '0 9 * * *',
    timezone: 'UTC', enabled: true, next_runs: [], last_status: '', run_once_at: null, last_slot: null,
    created_by_email: 'jj@dimagi.com', stopped: null, cli: '', sender_address: '', subject_pattern: '',
    turn_mode: '', ...over,
  }
}

describe('TriggersSection plain jobs', () => {
  it('lists a job as "Job: {name} from {repo}"', async () => {
    listTriggers.mockResolvedValueOnce([row({ job: 'sync-leads', job_repo: 'hal-jobs' })])
    render(<TriggersSection />)
    expect(await screen.findByText('Job: sync-leads from hal-jobs')).toBeTruthy()
  })

  it('says a job is waiting for a machine that can run jobs when it is queued', async () => {
    listTriggers.mockResolvedValueOnce([row({ job: 'sync-leads', job_repo: 'hal-jobs', last_status: 'queued' })])
    render(<TriggersSection />)
    expect(await screen.findByText('Waiting for a cloud machine that can run jobs.')).toBeTruthy()
  })

  it('does not say it for a job that has run, or for an ordinary schedule that is queued', async () => {
    listTriggers.mockResolvedValueOnce([
      row({ id: 1, job: 'sync-leads', job_repo: 'hal-jobs', last_status: 'done' }),
      row({ id: 2, name: 'Weekly', prompt: '/hal:turn', last_status: 'queued' }),
    ])
    render(<TriggersSection />)
    await screen.findByText('Weekly')
    expect(screen.queryByText('Waiting for a cloud machine that can run jobs.')).toBeNull()
    expect(screen.queryByText(/^Job: /)).toBe(screen.queryByText('Job: sync-leads from hal-jobs'))
  })

  it('opens a job read-only for someone who is not an admin', async () => {
    isAdmin = false
    listTriggers.mockResolvedValueOnce([row({ job: 'sync-leads', job_repo: 'hal-jobs' })])
    render(<TriggersSection />)
    fireEvent.click(await screen.findByText('Edit'))
    expect(await screen.findByText('Only an admin of hal can change a job.')).toBeTruthy()
  })

  it('leaves an ordinary schedule as it was', async () => {
    isAdmin = false
    listTriggers.mockResolvedValueOnce([row({ name: 'Weekly', prompt: '/hal:turn' })])
    render(<TriggersSection />)
    fireEvent.click(await screen.findByText('Edit'))
    await waitFor(() => expect(screen.getByPlaceholderText('/echo:manager-report')).toBeTruthy())
    expect(screen.queryByText('Only an admin of hal can change a job.')).toBeNull()
    expect(screen.queryByText('Run a job (no AI)')).toBeNull()
  })
})
