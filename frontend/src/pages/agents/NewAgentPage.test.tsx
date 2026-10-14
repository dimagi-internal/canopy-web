// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

// Mocked at the api modules: the push, the GitHub reads and the gates are the
// server's (tests/test_agent_creation.py). What is pinned here is the page's
// order of operations — check, push, THEN register — and that it hands the
// person the links to fix a repo canopy cannot see.
vi.mock('@/api/agentCreation', async (orig) => ({
  ...(await orig<typeof import('@/api/agentCreation')>()),
  checkScaffoldRepo: vi.fn(),
  scaffoldAgent: vi.fn(),
  registerAgent: vi.fn(),
}))
vi.mock('@/api/github', () => ({
  getGitHubConnection: vi.fn(async () => ({
    configured: true, connected: true, github_login: 'olive', needs_reconnect: false, install_url: '',
  })),
  listGitHubInstallations: vi.fn(async () => [
    { installation_id: 7, account_login: 'olive', account_type: 'User', is_org: false,
      repository_selection: 'selected', repositories: [], repository_count: 0 },
  ]),
  gitHubConnectPath: () => '/auth/github/start/',
}))
let role: string | null = 'editor'
vi.mock('@/workspace/WorkspaceProvider', () => ({ useWorkspaceRole: () => role }))

import { checkScaffoldRepo, registerAgent, scaffoldAgent } from '@/api/agentCreation'
import { NewAgentPage, suggestSlug } from './NewAgentPage'

const check = checkScaffoldRepo as unknown as ReturnType<typeof vi.fn>
const scaffold = scaffoldAgent as unknown as ReturnType<typeof vi.fn>
const register = registerAgent as unknown as ReturnType<typeof vi.fn>

function mount() {
  return render(
    <MemoryRouter initialEntries={['/w/scouts/agents/new']}>
      <Routes>
        <Route path="/w/:workspace/agents/new" element={<NewAgentPage />} />
        <Route path="/w/:workspace/agents/:slug/settings" element={<p>settings page</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

function fillAbout() {
  fireEvent.change(screen.getByPlaceholderText('Scout'), { target: { value: 'Scout Bot' } })
  fireEvent.change(screen.getByPlaceholderText('Find good first issues for new contributors.'), {
    target: { value: 'Find good first issues.' },
  })
}

beforeEach(() => {
  role = 'editor'
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('suggestSlug', () => {
  it('lowercases and hyphenates; the server stays the authority', () => {
    expect(suggestSlug('  Scout Bot!! ')).toBe('scout-bot')
  })
})

describe('NewAgentPage', () => {
  it('checks the repo, pushes, then registers, and lands on the setup checklist', async () => {
    check.mockResolvedValue({ repo: 'olive/scout-bot', state: 'ready', default_branch: 'main', detail: '', fix_url: '', new_repo_url: '' })
    scaffold.mockResolvedValue({ repo: 'olive/scout-bot', repo_url: 'https://github.com/olive/scout-bot', repo_ref: 'main', adopted: false, files: 21 })
    register.mockResolvedValue(undefined)
    mount()
    fillAbout()
    await screen.findByText('olive/scout-bot is empty and canopy can push to it.', {}, { timeout: 3000 })
    expect(check).toHaveBeenLastCalledWith('scouts', 'scout-bot', 'olive/scout-bot')

    fireEvent.click(screen.getByRole('button', { name: 'Create agent' }))
    await screen.findByText('settings page')
    const sent = scaffold.mock.calls[0][0]
    expect(sent).toMatchObject({ workspace: 'scouts', slug: 'scout-bot', name: 'Scout Bot', repo: 'olive/scout-bot' })
    expect(register).toHaveBeenCalledWith(sent, expect.objectContaining({ repo_ref: 'main' }))
    expect(scaffold.mock.invocationCallOrder[0]).toBeLessThan(register.mock.invocationCallOrder[0])
  })

  it('gives both fixes for a repo canopy cannot see, and will not create', async () => {
    check.mockResolvedValue({
      repo: 'olive/scout-bot', state: 'missing', default_branch: 'main', detail: 'canopy cannot see olive/scout-bot.',
      fix_url: 'https://github.com/apps/canopy-agents/installations/new',
      new_repo_url: 'https://github.com/new?owner=olive&name=scout-bot',
    })
    mount()
    fillAbout()
    const create = await screen.findByRole('link', { name: 'Create olive/scout-bot on GitHub ↗' }, { timeout: 3000 })
    expect(create.getAttribute('href')).toBe('https://github.com/new?owner=olive&name=scout-bot')
    expect(screen.getByRole('link', { name: 'Give canopy access to it ↗' }).getAttribute('href')).toBe(
      'https://github.com/apps/canopy-agents/installations/new',
    )
    expect((screen.getByRole('button', { name: 'Create agent' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('tells a viewer it needs the editor role instead of offering a form', async () => {
    role = 'viewer'
    mount()
    await waitFor(() => expect(screen.getByText(/needs the editor role/)).toBeTruthy())
    expect(screen.queryByRole('button', { name: 'Create agent' })).toBeNull()
  })
})
