// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

const ONLY_ADMIN = 'Only an admin of hal can change a job.'
const when = new Date()
const job = {
  id: 1, agent_slug: 'hal', name: 'Nightly', prompt: '', job: 'sync', job_repo: 'hal-jobs',
  cron: '0 9 * * *', timezone: 'UTC', enabled: true, next_runs: [], last_status: '', notify: [],
  routing: 'cloud_only', grace_minutes: 125, always_run: false, cli: '', run_once_at: null,
}
vi.mock('@/api/schedules', () => ({
  getScheduleWeek: vi.fn(async () => [
    { schedule: job, workspace_slug: 'connect', fires: [when.toISOString()] },
  ]),
  previewCron: vi.fn(async () => []),
}))
vi.mock('@/api/triggers', () => ({
  createTrigger: vi.fn(), updateTrigger: vi.fn(), deleteTrigger: vi.fn(),
}))
let agent: () => Promise<{ is_admin: boolean }> = async () => ({ is_admin: true })
vi.mock('@/api/agents', () => ({ getAgent: vi.fn(() => agent()) }))

const { ScheduleCalendar } = await import('./ScheduleCalendar')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  agent = async () => ({ is_admin: true })
})

async function open() {
  render(<ScheduleCalendar showWorkspaceFilter={false} />)
  fireEvent.click(await screen.findByText('Nightly'))
}

describe('ScheduleCalendar and a job', () => {
  it('lets an admin of the agent edit a job, as the Triggers tab does', async () => {
    await open()
    expect(await screen.findByLabelText(/which job/i)).toBeTruthy()
    expect(screen.queryByText(ONLY_ADMIN)).toBeNull()
  })

  it('shows anyone else the job read-only', async () => {
    agent = async () => ({ is_admin: false })
    await open()
    expect(await screen.findByText(ONLY_ADMIN)).toBeTruthy()
  })

  it('shows it read-only when it cannot tell', async () => {
    agent = async () => { throw new Error('offline') }
    await open()
    expect(await screen.findByText(ONLY_ADMIN)).toBeTruthy()
  })
})
