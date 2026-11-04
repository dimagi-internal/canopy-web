// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

const getApp = vi.fn()
const listMine = vi.fn()
const install = vi.fn()
const configure = vi.fn()
const accept = vi.fn()
const remove = vi.fn()
vi.mock('@/api/localApps', () => ({
  getLocalApp: (w: string, k: string) => getApp(w, k),
  listMyLocalApps: () => listMine(),
  installLocalApp: (w: string, k: string, body: unknown) => install(w, k, body),
  configureLocalApp: (id: number, s: unknown) => configure(id, s),
  acceptLocalAppChanges: (id: number, d: string) => accept(id, d),
  removeLocalApp: (id: number) => remove(id),
  stopLocalAppRun: vi.fn(),
  settingsSpecOf: (raw: unknown) => raw,
}))

import { ApiError } from '@/api/problem'
import LocalAppDetailPage from './LocalAppDetailPage'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

const SETTINGS = {
  agent: { type: 'agent', label: 'Which agent gives feedback', help: 'It gets the transcript.', required: true },
  brief: { type: 'longtext', label: 'What kind of feedback', help: '', required: false },
  title: { type: 'text', label: 'Session title', help: '', required: false, default: 'Meeting' },
  quiet: { type: 'bool', label: 'Stay quiet', help: '', required: false },
}
const APP = {
  key: 'meeting-assistant', name: 'Meeting assistant', description: 'Live feedback from an agent.', version: '0.2.0', publisher: 'canopy',
  notice: "What's said in this meeting leaves your Mac.", disclosure: { reads: ['the transcript'], sends: ['transcript text to a session'] },
  settings: SETTINGS, digest: 'digest-1', installs_with: ['Superwhisper transcript'],
  agents: [{ slug: 'hal', name: 'Hal' }, { slug: 'ada', name: 'Ada' }],
  computers: [{ id: 'r1', name: 'Jon laptop' }],
  versions: [{ version: '0.2.0', released_at: '2026-11-03T10:00:00Z', changelog: 'Better batches.', revoked: false }], enabled: true,
}
const INSTALL = {
  id: 3, key: 'meeting-assistant', name: 'Meeting assistant', workspace: 'dimagi', computer: { id: 'r1', name: 'Jon laptop' },
  settings: { agent: 'hal', brief: 'be brief' }, settings_spec: SETTINGS, agents: APP.agents, version: '0.2.0',
  needs_acceptance: false, changes: [], digest: 'digest-1', refusal: null, last_run: null,
}

function open() {
  return render(
    <MemoryRouter initialEntries={['/w/dimagi/apps/local/meeting-assistant']}>
      <Routes><Route path="/w/:workspace/apps/local/:key" element={<LocalAppDetailPage />} /></Routes>
    </MemoryRouter>,
  )
}

describe('LocalAppDetailPage', () => {
  it('says what it does on your computer, and that the page cannot start it', async () => {
    getApp.mockResolvedValue(APP)
    listMine.mockResolvedValue([])
    open()
    const block = await screen.findByTestId('local-app-disclosure')
    expect(block.textContent).toContain("What's said in this meeting leaves your Mac.")
    expect(block.textContent).toContain('the transcript')
    expect(block.textContent).toContain('transcript text to a session')
    expect(block.textContent).toContain('Canopy reviewed this code')
    expect(block.textContent).toContain('can read what you can read')
    expect(screen.getByTestId('local-app-installs-with').textContent).toContain('Superwhisper transcript')
    expect(screen.getByText(/start it from the menu bar/)).toBeTruthy()
    expect(screen.getByText('Better batches.')).toBeTruthy()
  })

  it('sends the digest it showed and the settings by type, and shows the install once it lands', async () => {
    getApp.mockResolvedValue(APP)
    listMine.mockResolvedValueOnce([]).mockResolvedValue([INSTALL])
    install.mockResolvedValue(INSTALL)
    open()
    fireEvent.change(await screen.findByLabelText(/Which agent gives feedback/), { target: { value: 'ada' } })
    fireEvent.change(screen.getByLabelText(/What kind of feedback/), { target: { value: 'be kind' } })
    fireEvent.click(screen.getByLabelText('Stay quiet'))
    expect((screen.getByLabelText(/Session title/) as HTMLInputElement).value).toBe('Meeting')
    fireEvent.click(screen.getByRole('button', { name: 'Install' }))
    await waitFor(() => expect(install).toHaveBeenCalled())
    expect(install).toHaveBeenCalledWith('dimagi', 'meeting-assistant', {
      runner_id: 'r1',
      accept_digest: 'digest-1',
      settings: { agent: 'ada', brief: 'be kind', title: 'Meeting', quiet: true },
    })
    expect(await screen.findByTestId('local-app-installed')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Configure' })).toBeTruthy()
    // That was the only computer, so there is nowhere left to install it.
    expect(screen.queryByRole('form', { name: 'Install' })).toBeNull()
  })

  it('leaves out a blank optional agent, which the server would read as an agent named ""', async () => {
    getApp.mockResolvedValue({
      ...APP,
      settings: { agent: { type: 'agent', label: 'Agent', help: '', required: false }, brief: SETTINGS.brief },
    })
    listMine.mockResolvedValue([])
    install.mockResolvedValue(INSTALL)
    open()
    await screen.findByLabelText(/^Agent/)
    fireEvent.click(screen.getByRole('button', { name: 'Install' }))
    await waitFor(() => expect(install).toHaveBeenCalled())
    expect(install.mock.calls[0][2].settings).toEqual({})
  })

  it('says how to get the runner when you have no computer, and offers no form', async () => {
    getApp.mockResolvedValue({ ...APP, computers: [] })
    listMine.mockResolvedValue([])
    open()
    const empty = await screen.findByTestId('local-app-no-computer')
    expect(empty.textContent).toContain('no computer')
    expect(screen.getByRole('link', { name: 'How to get the runner' }).getAttribute('href')).toBe('/get-started#runner')
    expect(screen.queryByRole('button', { name: 'Install' })).toBeNull()
  })

  it('shows a refusal in the words of the API and redraws what changed while they read', async () => {
    getApp.mockResolvedValueOnce(APP).mockResolvedValue({ ...APP, digest: 'digest-2', notice: 'Now it also reads your calendar.' })
    listMine.mockResolvedValue([])
    install.mockRejectedValueOnce(new ApiError(409, 'this app changed while you were reading. Read it again.'))
    install.mockResolvedValue(INSTALL)
    open()
    fireEvent.change(await screen.findByLabelText(/Which agent gives feedback/), { target: { value: 'ada' } })
    fireEvent.click(screen.getByRole('button', { name: 'Install' }))
    expect((await screen.findByRole('alert')).textContent).toBe('this app changed while you were reading. Read it again.')
    expect((await screen.findByTestId('local-app-disclosure')).textContent).toContain('Now it also reads your calendar.')
    expect((screen.getByLabelText(/Which agent gives feedback/) as HTMLSelectElement).value).toBe('ada')
    fireEvent.click(screen.getByRole('button', { name: 'Install' }))
    await waitFor(() => expect(install).toHaveBeenCalledTimes(2))
    expect(install.mock.calls[1][2].accept_digest).toBe('digest-2')
  })

  it('offers another computer once one has it, and never the one that has it', async () => {
    getApp.mockResolvedValue({ ...APP, computers: [...APP.computers, { id: 'r2', name: 'Spare laptop' }] })
    listMine.mockResolvedValue([INSTALL])
    open()
    const picker = (await screen.findByLabelText('Computer')) as HTMLSelectElement
    expect(Array.from(picker.options).map((o) => o.text)).toEqual(['Spare laptop'])
    expect(screen.getByText('Install on another computer')).toBeTruthy()
  })

  it('accepting a new version sends the digest it was shown, after showing what changed', async () => {
    getApp.mockResolvedValue(APP)
    listMine.mockResolvedValueOnce([{
      ...INSTALL, version: '0.1.0', needs_acceptance: true, digest: 'digest-new', changes: ['It now also sends your calendar'],
    }]).mockResolvedValue([INSTALL])
    accept.mockResolvedValue(INSTALL)
    open()
    const block = await screen.findByTestId('local-app-acceptance')
    expect(block.textContent).toContain('It now also sends your calendar')
    expect(block.textContent).toContain('stays on 0.1.0')
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    await waitFor(() => expect(accept).toHaveBeenCalledWith(3, 'digest-new'))
    await waitFor(() => expect(screen.queryByTestId('local-app-acceptance')).toBeNull())
  })

  it('configuring sends only what changed, so an agent the person lost does not block another setting', async () => {
    getApp.mockResolvedValue(APP)
    listMine.mockResolvedValue([{ ...INSTALL, settings: { agent: 'gone-agent', brief: 'be brief' }, refusal: "you can't use the agent 'gone-agent' in this workspace" }])
    configure.mockResolvedValue(INSTALL)
    open()
    expect((await screen.findByTestId('local-app-refusal')).textContent).toContain('gone-agent')
    fireEvent.click(screen.getByRole('button', { name: 'Configure' }))
    expect((screen.getByLabelText(/Which agent gives feedback/) as HTMLSelectElement).value).toBe('gone-agent')
    fireEvent.change(screen.getByLabelText(/What kind of feedback/), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(configure).toHaveBeenCalled())
    expect(configure.mock.calls[0][1]).toEqual({ brief: '', title: 'Meeting', quiet: false })
  })

  it('removes only after a yes', async () => {
    getApp.mockResolvedValue(APP)
    listMine.mockResolvedValue([INSTALL])
    remove.mockResolvedValue(undefined)
    open()
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    fireEvent.click(await screen.findByRole('button', { name: 'Remove' }))
    expect(remove).not.toHaveBeenCalled()
    confirm.mockReturnValue(true)
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(remove).toHaveBeenCalledWith(3))
  })

  it('shows the API words when the app cannot be read', async () => {
    getApp.mockRejectedValue(new ApiError(404, 'no such app'))
    listMine.mockResolvedValue([])
    open()
    expect(await screen.findByText('no such app')).toBeTruthy()
  })
})
