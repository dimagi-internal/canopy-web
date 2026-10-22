// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

// Mocked at the api modules: the push, the token checks and the gates are the
// server's (tests/test_agent_creation.py, tests/test_agent_delegations.py).
// What is pinned here is the page's order — check, push, register, THEN lend
// the same token — and that a person without a repo or token is told how.
vi.mock('@/api/agentCreation', async (orig) => ({
  ...(await orig<typeof import('@/api/agentCreation')>()),
  checkScaffoldRepo: vi.fn(),
  scaffoldAgent: vi.fn(),
  registerAgent: vi.fn(),
}))
vi.mock('@/api/agents', () => ({ setAgentGitHub: vi.fn() }))
let role: string | null = 'editor'
vi.mock('@/workspace/WorkspaceProvider', () => ({ useWorkspaceRole: () => role }))

import { checkScaffoldRepo, registerAgent, scaffoldAgent } from '@/api/agentCreation'
import { setAgentGitHub } from '@/api/agents'
import { NewAgentPage, suggestSlug } from './NewAgentPage'

const check = checkScaffoldRepo as unknown as ReturnType<typeof vi.fn>
const scaffold = scaffoldAgent as unknown as ReturnType<typeof vi.fn>
const register = registerAgent as unknown as ReturnType<typeof vi.fn>
const lend = setAgentGitHub as unknown as ReturnType<typeof vi.fn>

const LINKS = {
  new_repo_url: 'https://github.com/new?owner=olive&name=scout-bot',
  token_url: 'https://github.com/settings/personal-access-tokens/new?target_name=olive',
}

function answer(state: string, over: Record<string, unknown> = {}) {
  return { repo: 'olive/scout-bot', state, default_branch: 'main', detail: '', login: '', files_to_add: 0, suggested_slug: '', ...LINKS, ...over }
}

function Landed() {
  return <p data-testid="landed">{useLocation().search}</p>
}

function mount() {
  return render(
    <MemoryRouter initialEntries={['/w/scouts/agents/new']}>
      <Routes>
        <Route path="/w/:workspace/agents/new" element={<NewAgentPage />} />
        <Route path="/w/:workspace/agents/:slug/settings" element={<Landed />} />
      </Routes>
    </MemoryRouter>,
  )
}

function fill() {
  fireEvent.change(screen.getByPlaceholderText('Scout'), { target: { value: 'Scout Bot' } })
  fireEvent.change(screen.getByPlaceholderText('Find good first issues for new contributors.'), {
    target: { value: 'Find good first issues.' },
  })
  fireEvent.change(screen.getByPlaceholderText('dimagi-internal'), { target: { value: 'olive' } })
}

beforeEach(() => {
  role = 'editor'
  try {
    window.localStorage.removeItem('canopy:new-agent-owner')
  } catch {
    // no storage in this environment
  }
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
  it('shows how to make the repo and token before there is a token', async () => {
    check.mockResolvedValue(answer('no_token'))
    mount()
    expect(await screen.findByText(/Still needed: a name, a slug, a mandate, a GitHub token/)).toBeTruthy()
    fill()
    const create = await screen.findByRole('link', { name: 'Create olive/scout-bot on GitHub ↗' }, { timeout: 3000 })
    expect(create.getAttribute('href')).toBe(LINKS.new_repo_url)
    expect(screen.getByRole('link', { name: 'Create the token on GitHub' }).getAttribute('href')).toBe(LINKS.token_url)
    expect(check).toHaveBeenLastCalledWith('scouts', 'scout-bot', 'olive/scout-bot', '')
  })

  it('checks with the token, pushes, registers, lends the same token, then lands on setup', async () => {
    check.mockResolvedValue(answer('ready', { login: 'olive' }))
    scaffold.mockResolvedValue({ repo: 'olive/scout-bot', repo_url: 'https://github.com/olive/scout-bot', repo_ref: 'main', adopted: false, files: 21 })
    register.mockResolvedValue(undefined)
    lend.mockResolvedValue({})
    mount()
    fill()
    fireEvent.change(screen.getByPlaceholderText('github_pat_…'), { target: { value: 'github_pat_olive' } })
    await screen.findByText('olive/scout-bot is empty; pushing as @olive.', {}, { timeout: 3000 })
    expect(check).toHaveBeenLastCalledWith('scouts', 'scout-bot', 'olive/scout-bot', 'github_pat_olive')

    fireEvent.click(screen.getByRole('button', { name: 'Create agent' }))
    expect((await screen.findByTestId('landed')).textContent).toBe('?created=olive%2Fscout-bot')
    const sent = scaffold.mock.calls[0][0]
    expect(sent).toMatchObject({ slug: 'scout-bot', repo: 'olive/scout-bot', token: 'github_pat_olive' })
    expect(register).toHaveBeenCalledWith(sent, expect.objectContaining({ repo_ref: 'main' }))
    expect(lend).toHaveBeenCalledWith('scout-bot', 'github_pat_olive')
    expect(scaffold.mock.invocationCallOrder[0]).toBeLessThan(register.mock.invocationCallOrder[0])
    expect(register.mock.invocationCallOrder[0]).toBeLessThan(lend.mock.invocationCallOrder[0])
  })

  it('says when lending the token fails, and still offers the setup', async () => {
    check.mockResolvedValue(answer('ready', { login: 'olive' }))
    scaffold.mockResolvedValue({ repo: 'olive/scout-bot', repo_url: 'https://github.com/olive/scout-bot', repo_ref: 'main', adopted: false, files: 22 })
    register.mockResolvedValue(undefined)
    lend.mockRejectedValue(new Error('Saving the token failed: it cannot push to olive/scout-bot.'))
    mount()
    fill()
    fireEvent.change(screen.getByPlaceholderText('github_pat_…'), { target: { value: 'github_pat_olive' } })
    await screen.findByText('olive/scout-bot is empty; pushing as @olive.', {}, { timeout: 3000 })
    fireEvent.click(screen.getByRole('button', { name: 'Create agent' }))
    expect(await screen.findByText(/was created, but saving the token as its GitHub identity failed: Saving the token failed: it cannot push/)).toBeTruthy()
    expect(screen.queryByTestId('landed')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Open its setup' }))
    expect((await screen.findByTestId('landed')).textContent).toBe('?created=olive%2Fscout-bot')
  })

  it('will not create when the token cannot see the repo, and says why', async () => {
    check.mockResolvedValue(answer('missing', { detail: 'The token cannot see olive/scout-bot.' }))
    mount()
    fill()
    fireEvent.change(screen.getByPlaceholderText('github_pat_…'), { target: { value: 'github_pat_olive' } })
    await screen.findByText(/The token cannot see olive\/scout-bot\./, {}, { timeout: 3000 })
    expect((screen.getByRole('button', { name: 'Create agent' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText(/a repository the token can push to/)).toBeTruthy()
  })

  it('lets an earlier attempt be finished: a repo with some files counts as ready', async () => {
    check.mockResolvedValue(answer('update', { detail: 'olive/scout-bot already has 3 file(s). Canopy will add the 17 template file(s) it lacks…', files_to_add: 17 }))
    mount()
    fill()
    fireEvent.change(screen.getByPlaceholderText('github_pat_…'), { target: { value: 'github_pat_olive' } })
    await screen.findByText(/Canopy will add the 17 template file/, {}, { timeout: 3000 })
    expect((screen.getByRole('button', { name: 'Create agent' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it("offers the slug a repo already names its agent, and takes it on click", async () => {
    check.mockResolvedValue(answer('name_mismatch', { detail: "olive/scout-bot already holds the agent 'ranger'.", suggested_slug: 'ranger' }))
    mount()
    fill()
    fireEvent.change(screen.getByPlaceholderText('github_pat_…'), { target: { value: 'github_pat_olive' } })
    fireEvent.click(await screen.findByRole('button', { name: 'Use “ranger”' }, { timeout: 3000 }))
    expect((screen.getByPlaceholderText('scout') as HTMLInputElement).value).toBe('ranger')
  })

  it('tells a viewer it needs the editor role instead of offering a form', async () => {
    role = 'viewer'
    mount()
    await waitFor(() => expect(screen.getByText(/needs the editor role/)).toBeTruthy())
    expect(screen.queryByRole('button', { name: 'Create agent' })).toBeNull()
  })
})
