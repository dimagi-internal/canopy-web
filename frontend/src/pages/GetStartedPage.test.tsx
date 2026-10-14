// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

const authState = vi.hoisted(() => ({ value: { status: 'anonymous', user: null } as { status: string; user: unknown } }))
const workspaceState = vi.hoisted(() => ({ value: { workspaces: [] as Array<{ slug: string }>, active: null as string | null } }))
vi.mock('@/auth/AuthProvider', () => ({ useAuth: () => authState.value }))
vi.mock('@/workspace/WorkspaceProvider', () => ({ useWorkspace: () => workspaceState.value }))

const { GetStartedPage } = await import('./GetStartedPage')

function renderPage() {
  return render(
    <MemoryRouter>
      <GetStartedPage />
    </MemoryRouter>,
  )
}

const commands = () => screen.getAllByRole('code').map((c) => c.textContent)

function marketplace(plugins: Array<{ name: string; description?: string }>) {
  return { ok: true, json: async () => ({ name: 'x', plugins }) } as Response
}

beforeEach(() => {
  authState.value = { status: 'anonymous', user: null }
  workspaceState.value = { workspaces: [], active: null }
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

  it('signed out, shows the canopy install and the ACE / connect example', () => {
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    renderPage()
    expect(commands()).toEqual(
      expect.arrayContaining([
        '/plugin marketplace add dimagi-internal/canopy',
        '/plugin install canopy@canopy',
        '/canopy:setup',
        '/plugin install ace@canopy-connect',
        '/ace:setup',
      ]),
    )
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it("signed in, lists each workspace's agents under that workspace's catalog", async () => {
    authState.value = { status: 'authenticated', user: { email: 'a@dimagi.com' } }
    workspaceState.value = { workspaces: [{ slug: 'dimagi' }, { slug: 'connect' }, { slug: 'empty' }], active: 'dimagi' }
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url.endsWith('/w/dimagi/marketplace.json')) return marketplace([{ name: 'eva', description: 'Chief of staff' }])
        if (url.endsWith('/w/connect/marketplace.json')) return marketplace([{ name: 'ace' }, { name: 'echo' }])
        return marketplace([])
      }),
    )
    renderPage()
    const connect = await screen.findByRole('region', { name: 'Agents in canopy-connect' })
    expect(within(connect).getAllByRole('code').map((c) => c.textContent)).toEqual([
      '/plugin install ace@canopy-connect',
      '/plugin install echo@canopy-connect',
    ])
    const dimagi = screen.getByRole('region', { name: 'Agents in canopy-dimagi' })
    expect(within(dimagi).getByRole('code').textContent).toBe('/plugin install eva@canopy-dimagi')
    expect(screen.queryByRole('region', { name: 'Agents in canopy-empty' })).toBeNull()
    expect(commands()).toEqual(expect.arrayContaining(['/eva:setup', '/ace:setup', '/echo:setup']))
  })

  it('signed in, falls back to the example when no catalog can be read', async () => {
    authState.value = { status: 'authenticated', user: { email: 'a@dimagi.com' } }
    workspaceState.value = { workspaces: [{ slug: 'dimagi' }], active: 'dimagi' }
    const fetchSpy = vi.fn(async () => ({ ok: false, json: async () => ({}) }) as Response)
    vi.stubGlobal('fetch', fetchSpy)
    renderPage()
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    expect(commands()).toContain('/plugin install ace@canopy-connect')
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
