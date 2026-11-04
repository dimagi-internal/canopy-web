// @vitest-environment jsdom
// The review a failed plain job raises has no Start now; it offers Run now.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const detail = (over: Record<string, unknown>) => ({
  id: 1, key: 'HAL-9', url: '', workspace: 'connect', ext_id: '', kind: 'work',
  title: 'Scheduled job failed: nightly', next_action: '', status: 'suggested', category: 'triage',
  priority: 0, labels: [], owner: '', assigned: '', agent_slug: 'hal', ask_kind: 'review', ask_open: true,
  ask_body: 'The job failed.', job_schedule_id: null, human_why: [], actions: [], events: [], relations: [],
  subtasks: [], parent: null, turn: null, blocked: false, may_edit: true, status_names: {}, notes: '', plan: '',
  rationale: '', links: [], created_at: '2026-11-04T09:00:00Z', updated_at: '2026-11-04T09:00:00Z', ...over,
})

const getTask = vi.fn<(key: string) => Promise<unknown>>()
vi.mock('@/api/work', async (orig) => ({
  ...(await orig<typeof import('@/api/work')>()),
  getTask: (k: string) => getTask(k),
  listMilestones: vi.fn(async () => []),
}))
vi.mock('@/api/triggers', () => ({ runTriggerNow: vi.fn() }))
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getAgent: vi.fn(async () => ({ is_admin: true })),
}))
vi.mock('./useDirectory', () => ({
  useDirectory: () => ({ people: [], agents: [], labels: [], projects: [] }),
  invalidateDirectory: () => {},
}))
vi.mock('@/widget/useResource', () => ({ TASK_RESOURCE: 'tasks', useResource: () => {} }))

const { TaskDetailView } = await import('./TaskDetailView')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('TaskDetailView decision', () => {
  it("offers Run now and no Start now on a failed job's review", async () => {
    getTask.mockResolvedValue(detail({ job_schedule_id: 7 }))
    render(<MemoryRouter><TaskDetailView taskKey="HAL-9" /></MemoryRouter>)
    expect(await screen.findByText('Run now')).toBeTruthy()
    expect(screen.queryByText('Start now')).toBeNull()
  })

  it('keeps Start now on an ordinary review', async () => {
    getTask.mockResolvedValue(detail({ job_schedule_id: null }))
    render(<MemoryRouter><TaskDetailView taskKey="HAL-9" /></MemoryRouter>)
    expect(await screen.findByText('Start now')).toBeTruthy()
    expect(screen.queryByText('Run now')).toBeNull()
  })
})
