// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const setAgentRepository = vi.fn(async () => ({ definition: { repo_url: 'https://github.com/dimagi/muse', repo_ref: 'main' } }))
vi.mock('@/api/agents', () => ({ setAgentRepository: (...a: unknown[]) => setAgentRepository(...(a as [])) }))

const { AgentRepositoryControl } = await import('./AgentRepositoryControl')

afterEach(() => cleanup())

describe('AgentRepositoryControl', () => {
  it('saves just the repository, in place', async () => {
    render(<AgentRepositoryControl slug="muse" initialUrl="" initialRef="main" canEdit />)
    const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Repository URL'), { target: { value: 'git@github.com:dimagi/muse.git' } })
    fireEvent.click(save)
    await waitFor(() => expect(screen.getByText(/Saved\./)).toBeTruthy())
    expect(setAgentRepository).toHaveBeenCalledWith('muse', 'git@github.com:dimagi/muse.git', 'main')
    expect((screen.getByLabelText('Repository URL') as HTMLInputElement).value).toBe('https://github.com/dimagi/muse')
  })

  it('shows the link, not the form, to someone who cannot change it', () => {
    render(<AgentRepositoryControl slug="muse" initialUrl="https://github.com/dimagi/muse" initialRef="main" canEdit={false} />)
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
    expect(screen.getByRole('link').getAttribute('href')).toBe('https://github.com/dimagi/muse')
  })
})
