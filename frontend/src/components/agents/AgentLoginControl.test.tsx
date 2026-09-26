// @vitest-environment jsdom
//
// The agent's own login: shown to everyone, changeable by its owner or an
// admin, and a refusal shows the server's reason — "already the login of 'ace'"
// is how you learn another instance holds it.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { setAgentLogin } = vi.hoisted(() => ({ setAgentLogin: vi.fn() }))
vi.mock('@/api/agents', () => ({ setAgentLogin }))

const { AgentLoginControl } = await import('./AgentLoginControl')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentLoginControl', () => {
  it('says when an agent has no login, and offers no control to a non-admin', () => {
    render(<AgentLoginControl agentSlug="echo" initialLogin={null} canEdit={false} />)
    expect(screen.getByTestId('agent-login').textContent).toBe('Not linked')
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('links a login and shows it', async () => {
    setAgentLogin.mockResolvedValue({ login: { user_id: 9, name: 'Echo', email: 'echo@dimagi-ai.com' } })
    render(<AgentLoginControl agentSlug="echo" initialLogin={null} canEdit />)
    fireEvent.click(screen.getByRole('button', { name: 'Link a login' }))
    fireEvent.change(screen.getByLabelText('Login email'), { target: { value: 'echo@dimagi-ai.com' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.getByTestId('agent-login').textContent).toBe('echo@dimagi-ai.com'))
    expect(setAgentLogin).toHaveBeenCalledWith('echo', 'echo@dimagi-ai.com')
  })

  it('shows why a login was refused', async () => {
    setAgentLogin.mockRejectedValue(new Error("ace@dimagi-ai.com is already the login of 'ace'"))
    render(<AgentLoginControl agentSlug="ace-staging" initialLogin={null} canEdit />)
    fireEvent.click(screen.getByRole('button', { name: 'Link a login' }))
    fireEvent.change(screen.getByLabelText('Login email'), { target: { value: 'ace@dimagi-ai.com' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain("already the login of 'ace'"))
  })
})
