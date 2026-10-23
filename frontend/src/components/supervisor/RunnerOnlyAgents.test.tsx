// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RunnerOut } from '@/api/harness'

// A runner admin allowlists agents on a box: a switch, then an explicit list.

const setRunnerOnlyAgents = vi.fn()
const getRunnerOnlyAgents = vi.fn()
vi.mock('@/api/harness', () => ({
  setRunnerOnlyAgents: (...a: unknown[]) => setRunnerOnlyAgents(...a),
  getRunnerOnlyAgents: (...a: unknown[]) => getRunnerOnlyAgents(...a),
}))

const { RunnerOnlyAgents } = await import('./RunnerOnlyAgents')

const open = { id: 'r1', name: 'cloud-zdr-1', only_agents: [] } as unknown as RunnerOut
const choices = {
  agents: [],
  choices: [
    { slug: 'ace', name: 'ACE', workspace: 'connect' },
    { slug: 'labsdata', name: 'labsdata', workspace: 'connect' },
  ],
}
const onChange = vi.fn()

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RunnerOnlyAgents', () => {
  it('is off for an unrestricted box, with no list', () => {
    render(<RunnerOnlyAgents runner={open} onChange={onChange} />)
    expect(screen.getByRole('switch', { name: 'Allowlist agents' }).getAttribute('aria-checked')).toBe('false')
    expect(screen.queryByTestId('runner-only-agents-list')).toBeNull()
    expect(getRunnerOnlyAgents).not.toHaveBeenCalled()
  })

  it('turning it on lists the agents; ticking one saves it', async () => {
    getRunnerOnlyAgents.mockResolvedValue(choices)
    setRunnerOnlyAgents.mockResolvedValue({ ...open, only_agents: ['labsdata'] })
    render(<RunnerOnlyAgents runner={open} onChange={onChange} />)
    fireEvent.click(screen.getByRole('switch', { name: 'Allowlist agents' }))
    fireEvent.click(await screen.findByTestId('runner-only-agents-labsdata'))
    await waitFor(() => expect(setRunnerOnlyAgents).toHaveBeenCalledWith('r1', ['labsdata']))
    await waitFor(() => expect(onChange).toHaveBeenCalled())
  })

  it('turning it off clears the list', async () => {
    getRunnerOnlyAgents.mockResolvedValue({ ...choices, agents: ['labsdata'] })
    setRunnerOnlyAgents.mockResolvedValue(open)
    const dedicated = { ...open, only_agents: ['labsdata'] } as unknown as RunnerOut
    render(<RunnerOnlyAgents runner={dedicated} onChange={onChange} />)
    expect(screen.getByText(/Runs only labsdata/)).toBeTruthy()
    fireEvent.click(screen.getByRole('switch', { name: 'Allowlist agents' }))
    await waitFor(() => expect(setRunnerOnlyAgents).toHaveBeenCalledWith('r1', []))
  })

  it('shows the server error', async () => {
    getRunnerOnlyAgents.mockResolvedValue(choices)
    setRunnerOnlyAgents.mockRejectedValue(new Error('cloud-zdr-1 cannot serve ace'))
    render(<RunnerOnlyAgents runner={open} onChange={onChange} />)
    fireEvent.click(screen.getByRole('switch', { name: 'Allowlist agents' }))
    fireEvent.click(await screen.findByTestId('runner-only-agents-ace'))
    expect(await screen.findByText(/cannot serve ace/)).toBeTruthy()
    expect(onChange).not.toHaveBeenCalled()
  })
})
