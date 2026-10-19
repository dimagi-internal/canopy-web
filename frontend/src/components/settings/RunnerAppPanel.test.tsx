// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { RunnerAppPage, RunnerAppPreference } from '@/api/runnerApp'

const getRunnerAppPreference = vi.fn<() => Promise<RunnerAppPreference>>()
const setRunnerAppPreference = vi.fn<(p: RunnerAppPage) => Promise<RunnerAppPreference>>()
vi.mock('@/api/runnerApp', () => ({ getRunnerAppPreference, setRunnerAppPreference }))

const { RunnerAppPanel } = await import('./RunnerAppPanel')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RunnerAppPanel', () => {
  it('shows the saved page and saves a new one', async () => {
    getRunnerAppPreference.mockResolvedValue({ opens_on: 'supervisor' })
    setRunnerAppPreference.mockResolvedValue({ opens_on: 'ide' })
    render(<RunnerAppPanel />)
    const select = (await screen.findByTestId('runner-app-select')) as HTMLSelectElement
    expect(select.value).toBe('supervisor')
    fireEvent.change(select, { target: { value: 'ide' } })
    await waitFor(() => expect(select.value).toBe('ide'))
    expect(setRunnerAppPreference).toHaveBeenCalledWith('ide')
  })

  it('renders nothing when the setting cannot be read', async () => {
    getRunnerAppPreference.mockRejectedValue(new Error('down'))
    const { container } = render(<RunnerAppPanel />)
    await waitFor(() => expect(getRunnerAppPreference).toHaveBeenCalled())
    expect(container.innerHTML).toBe('')
  })
})
