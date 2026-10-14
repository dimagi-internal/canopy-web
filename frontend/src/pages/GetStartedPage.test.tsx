// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const authState = vi.hoisted(() => ({ value: { status: 'anonymous', user: null } as { status: string; user: unknown } }))
const listAgents = vi.hoisted(() => vi.fn())
vi.mock('@/auth/AuthProvider', () => ({ useAuth: () => authState.value }))
vi.mock('@/api/agents', () => ({ listAgents }))

const { GetStartedPage } = await import('./GetStartedPage')

function renderPage() {
  return render(
    <MemoryRouter>
      <GetStartedPage />
    </MemoryRouter>,
  )
}

const commands = () => screen.getAllByRole('code').map((c) => c.textContent)

function agent(slug: string, workspace: string, description = '') {
  return { slug, name: slug.toUpperCase(), description, workspace }
}

beforeEach(() => {
  authState.value = { status: 'anonymous', user: null }
  listAgents.mockReset()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('GetStartedPage', () => {
  it('shows the three steps in order', () => {
    renderPage()
    const headings = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    expect(headings).toEqual([
      'Step 1: Install canopy',
      'Step 2: Install an agent',
      'Step 3: Set up the agent',
    ])
    expect(screen.getByText('Before you start')).toBeTruthy()
    expect(screen.getByText('Then just ask')).toBeTruthy()
    expect(screen.getByText('The canopy-web connector is separate')).toBeTruthy()
  })

  it('signed out, shows the canopy install and the ACE example from canopy-agents', () => {
    renderPage()
    expect(commands()).toEqual(
      expect.arrayContaining([
        '/plugin marketplace add dimagi-internal/canopy',
        '/plugin install canopy@canopy',
        '/canopy:setup',
        '/plugin install ace@canopy-agents',
        '/ace:setup',
      ]),
    )
    expect(listAgents).not.toHaveBeenCalled()
    expect(screen.getByText(/read access to each agent's GitHub repo/)).toBeTruthy()
  })

  it('signed in, lists the agents of every workspace, all from canopy-agents', async () => {
    authState.value = { status: 'authenticated', user: { email: 'a@dimagi.com' } }
    listAgents.mockResolvedValue({
      items: [agent('eva', 'dimagi', 'Chief of staff'), agent('ace', 'connect'), agent('echo', 'connect')],
      total: 3, offset: 0, limit: 200,
    })
    renderPage()
    const list = await screen.findByRole('list', { name: 'Agents you can install' })
    expect(within(list).getAllByRole('code').map((c) => c.textContent)).toEqual([
      '/plugin install eva@canopy-agents',
      '/plugin install ace@canopy-agents',
      '/plugin install echo@canopy-agents',
    ])
    expect(commands()).toEqual(expect.arrayContaining(['/eva:setup', '/ace:setup', '/echo:setup']))
    expect(commands().some((c) => c?.includes('canopy-dimagi') || c?.includes('canopy-connect'))).toBe(false)
  })

  it('signed in, falls back to the example when the agents cannot be read', async () => {
    authState.value = { status: 'authenticated', user: { email: 'a@dimagi.com' } }
    listAgents.mockRejectedValue(new Error('offline'))
    renderPage()
    await waitFor(() => expect(listAgents).toHaveBeenCalled())
    expect(commands()).toContain('/plugin install ace@canopy-agents')
  })

  it('the Copy button puts the command on the clipboard and says Copied', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    renderPage()
    fireEvent.click(screen.getByRole('button', { name: 'Copy /canopy:setup' }))
    expect(writeText).toHaveBeenCalledWith('/canopy:setup')
    expect(await screen.findByRole('button', { name: 'Copied /canopy:setup' })).toBeTruthy()
  })

  it('without a clipboard, Copy selects the command instead', () => {
    vi.stubGlobal('navigator', { ...navigator, clipboard: undefined })
    renderPage()
    fireEvent.click(screen.getByRole('button', { name: 'Copy /canopy:setup' }))
    expect(window.getSelection()?.toString()).toBe('/canopy:setup')
  })
})
