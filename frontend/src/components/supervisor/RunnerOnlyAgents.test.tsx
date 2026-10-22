// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RunnerOut } from '@/api/harness'

// A runner admin dedicates a box to a list of agents from the runner's page.

const setRunnerOnlyAgents = vi.fn()
vi.mock('@/api/harness', () => ({
  setRunnerOnlyAgents: (...a: unknown[]) => setRunnerOnlyAgents(...a),
}))

const { RunnerOnlyAgents } = await import('./RunnerOnlyAgents')

const open = { id: 'r1', name: 'cloud-zdr-1', only_agents: [] } as unknown as RunnerOut
const onChange = vi.fn()

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RunnerOnlyAgents', () => {
  it('says an undedicated box runs anything routed to it', () => {
    render(<RunnerOnlyAgents runner={open} onChange={onChange} />)
    expect(screen.getByText(/runs any agent routed to it/)).toBeTruthy()
    expect((screen.getByTestId('runner-only-agents-save') as HTMLButtonElement).disabled).toBe(true)
  })

  it('saves the typed list', async () => {
    setRunnerOnlyAgents.mockResolvedValue({ ...open, only_agents: ['labsdata'] })
    render(<RunnerOnlyAgents runner={open} onChange={onChange} />)
    fireEvent.change(screen.getByTestId('runner-only-agents-input'), { target: { value: 'labsdata' } })
    fireEvent.click(screen.getByTestId('runner-only-agents-save'))
    await waitFor(() => expect(setRunnerOnlyAgents).toHaveBeenCalledWith('r1', ['labsdata']))
    await waitFor(() => expect(onChange).toHaveBeenCalled())
  })

  it('shows the server error', async () => {
    setRunnerOnlyAgents.mockRejectedValue(new Error('cloud-zdr-1 cannot serve nope'))
    render(<RunnerOnlyAgents runner={open} onChange={onChange} />)
    fireEvent.change(screen.getByTestId('runner-only-agents-input'), { target: { value: 'nope' } })
    fireEvent.click(screen.getByTestId('runner-only-agents-save'))
    expect(await screen.findByText(/cannot serve nope/)).toBeTruthy()
    expect(onChange).not.toHaveBeenCalled()
  })
})
