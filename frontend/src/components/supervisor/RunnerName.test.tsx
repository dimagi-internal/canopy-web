// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RunnerOut } from '@/api/harness'

// A runner admin renames a runner from its Settings tab.

const setRunnerName = vi.fn()
vi.mock('@/api/harness', () => ({
  setRunnerName: (...a: unknown[]) => setRunnerName(...a),
}))

const { RunnerName } = await import('./RunnerName')

const box = { id: 'r1', name: 'cloud-ec2-2', paired_as: 'cloud-ec2-2' } as unknown as RunnerOut
const onChange = vi.fn()

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RunnerName', () => {
  it('saves a new name', async () => {
    setRunnerName.mockResolvedValue({ ...box, name: 'cloud-ec2-live' })
    render(<RunnerName runner={box} onChange={onChange} />)
    fireEvent.click(screen.getByTestId('runner-name-edit'))
    fireEvent.change(screen.getByTestId('runner-name-input'), { target: { value: '  cloud-ec2-live ' } })
    fireEvent.click(screen.getByTestId('runner-name-save'))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ ...box, name: 'cloud-ec2-live' }))
    expect(setRunnerName).toHaveBeenCalledWith('r1', 'cloud-ec2-live')
  })

  it('refuses a blank name without asking the server', () => {
    render(<RunnerName runner={box} onChange={onChange} />)
    fireEvent.click(screen.getByTestId('runner-name-edit'))
    fireEvent.change(screen.getByTestId('runner-name-input'), { target: { value: '   ' } })
    fireEvent.click(screen.getByTestId('runner-name-save'))
    expect(screen.getByTestId('runner-name-error').textContent).toBe('a name cannot be blank')
    expect(setRunnerName).not.toHaveBeenCalled()
  })

  it('says what the box paired as once renamed', () => {
    render(<RunnerName runner={{ ...box, name: 'cloud-ec2-live' } as RunnerOut} onChange={onChange} />)
    expect(screen.getByTestId('runner-name').textContent).toContain('Paired as cloud-ec2-2.')
  })
})
