// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

// The checklist decides nothing — the server computes every step — so what is
// worth pinning is that it renders what it is given: links for web steps, the
// command for the Claude Code step, and nothing once it is all done.
vi.mock('@/api/agentCreation', () => ({ getAgentSetup: vi.fn() }))

import { getAgentSetup } from '@/api/agentCreation'
import { AgentSetupChecklist } from './AgentSetupChecklist'

const setupMock = getAgentSetup as unknown as ReturnType<typeof vi.fn>

function step(key: string, status: string, over: Record<string, unknown> = {}) {
  return { key, title: key, status, detail: `${key} detail`, where: 'web', href: '', command: '', ...over }
}

function mount(hideWhenDone = false) {
  return render(
    <MemoryRouter>
      <AgentSetupChecklist slug="scout" hideWhenDone={hideWhenDone} />
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  setupMock.mockReset()
})

describe('AgentSetupChecklist', () => {
  it('links a web step and hands over the command for the Claude Code step', async () => {
    setupMock.mockResolvedValue([
      step('repo', 'done', { href: 'https://github.com/olive/scout' }),
      step('github', 'todo', { href: '/w/scouts/agents/scout/settings#credentials', action: 'Add a GitHub token' }),
      step('job', 'todo', { where: 'claude-code', command: 'git clone x && claude "go"' }),
    ])
    mount()
    expect(await screen.findByText('2 of 3 steps left.', { exact: false })).toBeTruthy()
    // The link says what it does, not a bare "Open".
    expect(screen.getByRole('link', { name: 'Add a GitHub token' }).getAttribute('href')).toBe(
      '/w/scouts/agents/scout/settings#credentials',
    )
    // A finished step offers nothing to click.
    expect(screen.queryByRole('link', { name: 'Open ↗' })).toBeNull()
    expect(screen.getByText('git clone x && claude "go"')).toBeTruthy()
    expect(screen.getByText('in Claude Code')).toBeTruthy()
  })

  it('disappears from Settings once nothing required is left', async () => {
    setupMock.mockResolvedValue([step('repo', 'done'), step('mailbox', 'optional')])
    const { container } = mount(true)
    await vi.waitFor(() => expect(setupMock).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r, 0))
    expect(container.querySelector('[data-testid="agent-setup-checklist"]')).toBeNull()
  })
})
