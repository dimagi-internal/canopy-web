// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

const getStore = vi.fn()
const listMine = vi.fn()
const setSwitch = vi.fn()
const stop = vi.fn()
vi.mock('@/api/localApps', () => ({
  getLocalAppStore: (w: string) => getStore(w),
  listMyLocalApps: () => listMine(),
  setLocalAppsSwitch: (w: string, on: boolean) => setSwitch(w, on),
  stopLocalAppRun: (id: string) => stop(id),
}))

import { LocalAppsSection } from './LocalAppsSection'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

const APP = { key: 'meeting-assistant', name: 'Meeting assistant', description: 'Live feedback.', version: '0.1.0', tagline: '', consumes: [], provides: [], installed: false }
const RUN = { id: 'run-1', version: '0.1.0', started_at: '', finished_at: null, state: 'listening', counts: { sent: 4 }, stop_reason: '', stop_requested: false, running: true }
const INSTALL = {
  id: 3, key: 'meeting-assistant', name: 'Meeting assistant', workspace: 'dimagi', computer: { id: 'r1', name: 'Jon laptop' },
  settings: {}, settings_spec: {}, agents: [], version: '0.1.0', needs_acceptance: true, changes: [], digest: 'd', refusal: null, last_run: RUN,
}

function open() {
  return render(
    <MemoryRouter initialEntries={['/w/dimagi/apps']}>
      <Routes><Route path="/w/:workspace/apps" element={<LocalAppsSection workspace="dimagi" />} /></Routes>
    </MemoryRouter>,
  )
}

describe('LocalAppsSection', () => {
  it('when off, shows someone who cannot switch it nothing at all', async () => {
    getStore.mockResolvedValue({ enabled: false, can_switch: false, apps: [] })
    const { container } = open()
    await waitFor(() => expect(getStore).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r))
    expect(container.textContent).toBe('')
    expect(listMine).not.toHaveBeenCalled()
  })

  it('when off, tells an admin what it offers and why it is off, and switches on in one click', async () => {
    getStore.mockResolvedValue({ enabled: false, can_switch: true, apps: [] })
    setSwitch.mockResolvedValue(undefined)
    open()
    expect((await screen.findByTestId('local-apps-off')).textContent).toContain('record conversations')
    const confirm = vi.spyOn(window, 'confirm')
    fireEvent.click(screen.getByRole('switch'))
    await waitFor(() => expect(setSwitch).toHaveBeenCalledWith('dimagi', true))
    expect(confirm).not.toHaveBeenCalled()
  })

  it('asks before an admin switches it off, and does nothing if they say no', async () => {
    getStore.mockResolvedValue({ enabled: true, can_switch: true, apps: [APP] })
    listMine.mockResolvedValue([])
    open()
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    fireEvent.click(await screen.findByRole('switch'))
    expect(confirm).toHaveBeenCalled()
    expect(setSwitch).not.toHaveBeenCalled()
    confirm.mockReturnValue(true)
    fireEvent.click(screen.getByRole('switch'))
    await waitFor(() => expect(setSwitch).toHaveBeenCalledWith('dimagi', false))
  })

  it('lists apps and marks one installed, with a link to the rest', async () => {
    getStore.mockResolvedValue({
      enabled: true, can_switch: false,
      apps: [APP, { ...APP, key: 'other', name: 'Other', installed: true }],
    })
    listMine.mockResolvedValue([])
    open()
    const link = await screen.findByRole('link', { name: 'Details and install' })
    expect(link.getAttribute('href')).toBe('/w/dimagi/apps/local/meeting-assistant')
    expect(screen.getByText('Installed')).toBeTruthy()
    expect(screen.getByText('Experimental')).toBeTruthy()
    expect(screen.queryByRole('switch')).toBeNull()
  })

  it("shows this workspace's installations only, with what's running, Stop and needs-acceptance", async () => {
    getStore.mockResolvedValue({ enabled: true, can_switch: false, apps: [APP] })
    listMine.mockResolvedValue([INSTALL, { ...INSTALL, id: 4, workspace: 'elsewhere', computer: { id: 'r2', name: 'Other laptop' } }])
    stop.mockResolvedValue(RUN)
    open()
    const table = await screen.findByTestId('local-app-installations')
    expect(table.textContent).toContain('Jon laptop')
    expect(table.textContent).not.toContain('Other laptop')
    expect(table.textContent).toContain('Running: listening (sent 4)')
    expect(screen.getByText('Needs your acceptance')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(stop).toHaveBeenCalledWith('run-1'))
  })

  it('offers no Stop for a run that is not running', async () => {
    getStore.mockResolvedValue({ enabled: true, can_switch: false, apps: [APP] })
    listMine.mockResolvedValue([{
      ...INSTALL, needs_acceptance: false,
      last_run: { ...RUN, running: false, finished_at: '2026-11-03T10:00:00Z', stop_reason: 'meeting_ended', counts: {} },
    }])
    open()
    expect((await screen.findByTestId('local-app-installations')).textContent).toContain('Ended: meeting ended')
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull()
  })
})
