// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const setAgentClis = vi.fn(async (_slug: string, clis: string[], models: Record<string, string>) => ({
  clis, cli_models: models,
}))
vi.mock('@/api/agents', () => ({ setAgentClis: (...a: unknown[]) => setAgentClis(...(a as [string, string[], Record<string, string>])) }))

const { AgentClisControl } = await import('./AgentClisControl')

afterEach(() => cleanup())

describe('AgentClisControl', () => {
  it('shows the default agent as Claude Code, read-only to a non-admin', () => {
    render(<AgentClisControl slug="echo" initialClis={['claude-code']} initialModels={{}} canEdit={false} />)
    expect(screen.getByTestId('agent-clis').textContent).toContain('Claude Code')
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('lists them in the agent’s order', () => {
    render(<AgentClisControl slug="echo" initialClis={['codex', 'claude-code']} initialModels={{}} canEdit={false} />)
    expect(screen.getByTestId('agent-clis').textContent).toMatch(/1\.\s*Codex.*2\.\s*Claude Code/)
  })

  it('lets an admin add Codex first with a model and saves it', async () => {
    render(<AgentClisControl slug="echo" initialClis={['claude-code']} initialModels={{}} canEdit />)
    fireEvent.click(screen.getByLabelText('Run on Codex'))
    fireEvent.click(screen.getByRole('button', { name: 'Prefer Codex' }))
    fireEvent.change(screen.getByLabelText('Codex model'), { target: { value: 'gpt-6.1-sol' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.getByText(/Saved\./)).toBeTruthy())
    expect(setAgentClis).toHaveBeenCalledWith('echo', ['codex', 'claude-code'], { codex: 'gpt-6.1-sol' })
  })

  it('shows the server’s reason when it refuses', async () => {
    setAgentClis.mockRejectedValueOnce(new Error('no runner serving this workspace has Codex yet'))
    render(<AgentClisControl slug="echo" initialClis={['claude-code']} initialModels={{}} canEdit />)
    fireEvent.click(screen.getByLabelText('Run on Codex'))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.getByTestId('agent-clis-error').textContent).toContain('has Codex yet'))
  })

  it('will not save an empty list', () => {
    render(<AgentClisControl slug="echo" initialClis={['claude-code']} initialModels={{}} canEdit />)
    fireEvent.click(screen.getByLabelText('Run on Claude Code'))
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
