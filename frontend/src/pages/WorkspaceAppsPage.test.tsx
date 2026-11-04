// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, matchRoutes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getStore = vi.fn()
const listMine = vi.fn()
const stop = vi.fn()
vi.mock('@/api/localApps', () => ({
  getLocalAppStore: () => getStore(),
  listMyLocalApps: () => listMine(),
  stopLocalAppRun: (id: string) => stop(id),
}))

const listInstalled = vi.fn()
const listStore = vi.fn()
vi.mock('@/api/workspaceApps', () => ({
  listInstalledApps: (w: string) => listInstalled(w),
  listAppStore: (w: string) => listStore(w),
  appStoreScreenshotUrl: () => '/shot.png',
  installApp: vi.fn(),
  uninstallApp: vi.fn(),
  updateInstalledApp: vi.fn(),
}))
vi.mock('@/workspace/WorkspaceProvider', async (original) => ({
  ...(await original<typeof import('@/workspace/WorkspaceProvider')>()),
  useWorkspaceList: () => [{ slug: 'dimagi', role: 'admin' }],
}))

import { NOT_PAGES } from '@/guide/coverage'
import { shouldServeShell } from '@/pwa/navigation-fallback'
import { routeTable } from '@/router'
import WorkspaceAppsPage from './WorkspaceAppsPage'

const HIRING_STORE = { key: 'hiring', name: 'Hiring', description: 'An applicant tracker.', tagline: '', publisher: 'canopy', version: '2.2.0', cover: 'c.png' }
const HIRING = { key: 'hiring', name: 'Hiring', description: 'An applicant tracker.', template: 'hiring', template_version: '2.2.0', live_version: 3, enabled: true, public: false, has_public_pages: true, has_draft: false, upgrade_available: null }
const APP = { key: 'meeting-assistant', name: 'Meeting assistant', description: 'Live feedback.', version: '0.1.0', tagline: '', installed: false }
const RUN = { id: 'run-1', version: '0.1.0', started_at: '', finished_at: null, state: 'listening', counts: { sent: 4 }, stop_reason: '', stop_requested: false, running: true }
const INSTALL = {
  id: 3, key: 'meeting-assistant', name: 'Meeting assistant', workspace: 'dimagi', computer: { id: 'r1', name: 'Jon laptop' },
  settings: {}, settings_spec: {}, agents: [], version: '0.1.0', needs_acceptance: true, changes: [], digest: 'd', refusal: null, last_run: RUN,
}

function Where() {
  const { pathname, search } = useLocation()
  return <div data-testid="where">{pathname + search}</div>
}

function open(path = '/w/dimagi/apps') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes><Route path="/w/:workspace/apps" element={<><WorkspaceAppsPage /><Where /></>} /></Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  listInstalled.mockResolvedValue([HIRING])
  listStore.mockResolvedValue([HIRING_STORE, { ...HIRING_STORE, key: 'other', name: 'Other' }])
  getStore.mockResolvedValue({ apps: [APP] })
  listMine.mockResolvedValue([INSTALL])
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('the Apps page', () => {
  it('opens on Workspace apps, and asks for nothing personal until that tab is open', async () => {
    open()
    expect(screen.getByRole('tab', { name: 'Workspace apps' }).getAttribute('aria-selected')).toBe('true')
    await screen.findAllByTestId('app-store-card')
    expect(getStore).not.toHaveBeenCalled()
    expect(listMine).not.toHaveBeenCalled()
  })

  it('moves between tabs through the URL', async () => {
    open()
    fireEvent.click(screen.getByRole('tab', { name: 'Personal apps' }))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/w/dimagi/apps?tab=personal'))
    await screen.findByText('Live feedback.')
    fireEvent.click(screen.getByRole('tab', { name: 'Workspace apps' }))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/w/dimagi/apps'))
  })

  it('draws both tabs through the same lists', async () => {
    const parts = async () => ({
      headings: (await screen.findAllByRole('heading', { level: 2 })).map((h) => h.textContent),
      list: await screen.findByTestId('app-installed-list'),
      row: await screen.findAllByTestId('app-installed-row'),
      grid: await screen.findByTestId('app-store-grid'),
      card: await screen.findAllByTestId('app-store-card'),
    })
    open()
    const workspace = await parts()
    const shape = [workspace.list.className, workspace.row[0].className, workspace.grid.className, workspace.card[0].className]
    expect(workspace.headings).toEqual(['Installed', 'App store'])
    cleanup()
    open('/w/dimagi/apps?tab=personal')
    const personal = await parts()
    expect(personal.headings).toEqual(['Installed', 'App store'])
    expect([personal.list.className, personal.row[0].className, personal.grid.className, personal.card[0].className]).toEqual(shape)
  })

  it('names no place in either tab, and has no switch', async () => {
    const { container } = open('/w/dimagi/apps?tab=personal')
    await screen.findByTestId('app-installed-list')
    expect(container.textContent?.toLowerCase()).not.toContain('runs on your computer')
    expect(container.textContent?.toLowerCase()).not.toContain('experimental')
    expect(screen.queryByRole('switch')).toBeNull()
    expect(screen.queryByRole('checkbox')).toBeNull()
  })
})

describe('Workspace apps', () => {
  it('keeps what an admin could do, with no Unreleased badge', async () => {
    const { container } = open()
    const row = (await screen.findAllByTestId('app-installed-row'))[0]
    expect(within(row).getByRole('link', { name: 'Hiring' }).getAttribute('href')).toBe('/w/dimagi/apps/hiring')
    expect(row.textContent).toContain('version 3 · template hiring 2.2.0')
    expect(within(row).getByLabelText('On')).toBeTruthy()
    expect(within(row).getByLabelText('Public pages')).toBeTruthy()
    expect(within(row).getByRole('button', { name: 'Remove' })).toBeTruthy()
    const [have, other] = await screen.findAllByTestId('app-store-card')
    expect(within(have).getAllByRole('link')[0].getAttribute('href')).toBe('/w/dimagi/apps/store?template=hiring')
    expect(have.querySelector('img')?.getAttribute('src')).toBe('/shot.png')
    expect(have.textContent).toContain('by canopy · 2.2.0')
    expect(have.textContent).toContain('Screenshots and details')
    expect(have.textContent).toContain('Installed')
    expect(within(other).getByRole('button', { name: 'Install' })).toBeTruthy()
    expect(container.textContent).not.toContain('Unreleased')
  })
})

describe('Personal apps', () => {
  it('badges each app Unreleased, and sends Install to the app page, where the computer is chosen', async () => {
    open('/w/dimagi/apps?tab=personal')
    const card = (await screen.findAllByTestId('app-store-card'))[0]
    expect(within(card).getByText('Unreleased')).toBeTruthy()
    expect(within(card).getByRole('link', { name: 'Details' }).getAttribute('href')).toBe('/w/dimagi/apps/personal/meeting-assistant')
    expect(within(card).getByRole('link', { name: 'Install' }).getAttribute('href')).toBe('/w/dimagi/apps/personal/meeting-assistant#install')
  })

  it('marks one already installed', async () => {
    getStore.mockResolvedValue({ apps: [{ ...APP, installed: true }] })
    open('/w/dimagi/apps?tab=personal')
    const card = (await screen.findAllByTestId('app-store-card'))[0]
    expect(within(card).getByText('Installed')).toBeTruthy()
    expect(within(card).queryByRole('link', { name: 'Install' })).toBeNull()
  })

  it("shows every copy, whichever workspace it lands in, with what's running, Stop and needs-acceptance", async () => {
    listMine.mockResolvedValue([INSTALL, { ...INSTALL, id: 5, workspace: 'elsewhere', needs_acceptance: false, last_run: null, computer: { id: 'r2', name: 'Other laptop' } }])
    stop.mockResolvedValue(RUN)
    open('/w/dimagi/apps?tab=personal')
    const [first, second] = await screen.findAllByTestId('app-installed-row')
    expect(first.textContent).toContain('Jon laptop · version 0.1.0')
    expect(first.textContent).toContain('Running: listening (sent 4)')
    expect(first.textContent).toContain('Unreleased')
    expect(within(first).getByRole('link', { name: 'needs your acceptance' }).getAttribute('href')).toBe('/w/dimagi/apps/personal/meeting-assistant')
    expect(second.textContent).toContain('Other laptop')
    expect(within(second).queryByRole('button', { name: 'Stop' })).toBeNull()
    fireEvent.click(within(first).getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(stop).toHaveBeenCalledWith('run-1'))
  })

  it('offers no Stop for a run that has ended', async () => {
    listMine.mockResolvedValue([{
      ...INSTALL, needs_acceptance: false,
      last_run: { ...RUN, running: false, finished_at: '2026-11-03T10:00:00Z', stop_reason: 'meeting_ended', counts: {} },
    }])
    open('/w/dimagi/apps?tab=personal')
    expect((await screen.findByTestId('app-installed-row')).textContent).toContain('Ended: meeting ended')
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull()
  })
})

describe('reaching it', () => {
  const leaf = (path: string) => matchRoutes(routeTable, path)?.at(-1)?.route.path

  it('routes personal pages ahead of a web app, and keeps /app and the web apps as they were', () => {
    expect(leaf('/w/dimagi/apps/personal/meeting-assistant')).toBe('/w/:workspace/apps/personal/:key')
    expect(leaf('/w/dimagi/apps/personal/review/9')).toBe('/w/:workspace/apps/personal/review/:id')
    expect(leaf('/w/dimagi/apps/store')).toBe('/w/:workspace/apps/store')
    expect(leaf('/w/dimagi/apps/hiring')).toBe('/w/:workspace/apps/:appKey/:page?')
    expect(leaf('/w/dimagi/apps/hiring/customize')).toBe('/w/:workspace/apps/:appKey/customize')
    expect(leaf('/app')).toBe('/app')
  })

  it('has no second page at the root: /apps only redirects, and the service worker lets it through', () => {
    expect(leaf('/apps')).toBe('/apps')
    expect(NOT_PAGES['/apps']).toContain('redirect')
    expect(leaf('/apps/local/x')).toBe('*')
    for (const path of ['/apps', '/apps/', '/canopy/apps']) expect(shouldServeShell(path)).toBe(true)
    expect(shouldServeShell('/apps-elsewhere')).toBe(false)
    expect(shouldServeShell('/apps/local/x')).toBe(false)
  })
})
