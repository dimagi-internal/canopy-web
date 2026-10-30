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
