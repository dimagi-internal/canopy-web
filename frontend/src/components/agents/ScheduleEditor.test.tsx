// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const createSchedule = vi.fn((..._args: unknown[]) => Promise.resolve({}))
vi.mock('@/api/schedules', () => ({
  createSchedule,
  updateSchedule: vi.fn(),
  deleteSchedule: vi.fn(),
  previewCron: vi.fn(() => Promise.resolve([])),
}))

const { ScheduleEditor } = await import('./ScheduleEditor')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ScheduleEditor agent CLI', () => {
  it('is not offered for an agent set up for one CLI, and is not sent', async () => {
    render(<ScheduleEditor agentSlug="ace" agentClis={['claude-code']} schedule={null}
                           onClose={vi.fn()} onSaved={vi.fn()} />)
    expect(screen.queryByTestId('schedule-cli')).toBeNull()
    fireEvent.change(screen.getByPlaceholderText('Weekly manager report'), { target: { value: 'Post' } })
    fireEvent.change(screen.getByPlaceholderText('/echo:manager-report'), { target: { value: '/ace:turn' } })
    fireEvent.click(screen.getByText(/^Save|^Create/))
    await waitFor(() => expect(createSchedule).toHaveBeenCalled())
    expect(createSchedule.mock.calls[0][1]).not.toHaveProperty('cli')
  })

  it('saves the CLI picked', async () => {
    render(<ScheduleEditor agentSlug="ace" agentClis={['claude-code', 'codex']} schedule={null}
                           onClose={vi.fn()} onSaved={vi.fn()} />)
    fireEvent.change(screen.getByPlaceholderText('Weekly manager report'), { target: { value: 'Post' } })
    fireEvent.change(screen.getByPlaceholderText('/echo:manager-report'), { target: { value: '/ace:turn' } })
    fireEvent.change(screen.getByTestId('schedule-cli'), { target: { value: 'codex' } })
    fireEvent.click(screen.getByText(/^Save|^Create/))
    await waitFor(() => expect(createSchedule).toHaveBeenCalled())
    expect(createSchedule.mock.calls[0][1]).toMatchObject({ name: 'Post', cli: 'codex' })
  })
})
