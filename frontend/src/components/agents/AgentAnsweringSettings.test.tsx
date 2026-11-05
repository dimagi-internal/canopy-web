// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.hoisted(() => {
  const Native = globalThis.Request
  globalThis.Request = class extends Native {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(typeof input === 'string' ? new URL(input, window.location.href) : input, init)
    }
  } as typeof Request
})

const getReadMode = vi.fn()
const putReadMode = vi.fn()
const resetReadMode = vi.fn()
const listConnectedSites = vi.fn()
const putConnectedSite = vi.fn()
const deleteConnectedSite = vi.fn()
const getAgentSettingsHistory = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getReadMode: (...a: unknown[]) => getReadMode(...a),
  putReadMode: (...a: unknown[]) => putReadMode(...a),
  resetReadMode: (...a: unknown[]) => resetReadMode(...a),
  listConnectedSites: (...a: unknown[]) => listConnectedSites(...a),
  putConnectedSite: (...a: unknown[]) => putConnectedSite(...a),
  deleteConnectedSite: (...a: unknown[]) => deleteConnectedSite(...a),
  getAgentSettingsHistory: (...a: unknown[]) => getAgentSettingsHistory(...a),
}))

const { AgentAnsweringSettings } = await import('./AgentAnsweringSettings')

const PROFILE = {
  description: 'Ask Hal.', entry: null, tools: ['Read', 'mcp__*canopy-web__who_is_asking'], bash: [],
  read_paths: ['{cwd}/**'], write_paths: ['{cwd}/.hal-read/*'],
}
const ROW = (over = {}) => ({
  site: 'connect-labs', key: 'connect', description: '', entry: null, tools: ['Read'], bash: [], read_paths: [],
  write_paths: [], ceiling: ['mcp__*connect_labs__*'], own_login: ['LABS_MCP_TOKEN'], sensitive: false, ...over,
})
const SITES = (sites: unknown[] = [], available: string[] = []) => ({
  sites, available, new_site_defaults: { tools: ['Read'], bash: [], read_paths: [], write_paths: [], ceiling: [], own_login: [], sensitive: false },
})

function show(canEdit = true) {
  return render(<AgentAnsweringSettings agentSlug="hal" agentName="Hal" canEdit={canEdit} />)
}

beforeEach(() => {
  vi.clearAllMocks()
  getReadMode.mockResolvedValue({ source: 'default', profile: PROFILE })
  listConnectedSites.mockResolvedValue(SITES())
  getAgentSettingsHistory.mockResolvedValue([])
})
afterEach(cleanup)

describe('read mode', () => {
  it('says it is on the default, in plain words, with no word the settings retired', async () => {
    const { container } = show()
    expect((await screen.findByTestId('read-mode')).textContent).toContain("Hal is on canopy's default.")
    expect(screen.getByTestId('read-mode-profile').textContent).toContain('Can write only to: {cwd}/.hal-read/*')
    await screen.findByText('Hal cannot be reached from any site.')
    expect(container.textContent).not.toMatch(/\bdoors?\b|capabilit|interface|ceiling|own_login|\bask\b/i)
  })

  it('customises from what it is now, and saves the form', async () => {
    putReadMode.mockResolvedValue({ source: 'custom', profile: { ...PROFILE, description: 'Changed.' } })
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Customise' }))
    const field = screen.getByLabelText('What it says it is for') as HTMLInputElement
    expect(field.value).toBe('Ask Hal.')
    fireEvent.change(field, { target: { value: 'Changed.' } })
    fireEvent.change(screen.getByLabelText(/Commands it may run/), { target: { value: 'canopy doctor\n\n' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByText(/Hal uses its own list/)
    expect(putReadMode).toHaveBeenCalledWith('hal', expect.objectContaining({ description: 'Changed.', bash: ['canopy doctor'], entry: null }))
  })

  it('goes back to the default', async () => {
    getReadMode.mockResolvedValue({ source: 'custom', profile: PROFILE })
    resetReadMode.mockResolvedValue({ source: 'default', profile: PROFILE })
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Go back to the default' }))
    await screen.findByText(/on canopy's default/)
  })

  it('shows why a save was refused and keeps the form', async () => {
    putReadMode.mockRejectedValue(new Error('hal is set up in a way these settings cannot hold'))
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Customise' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect((await screen.findByRole('alert')).textContent).toContain('cannot hold')
    expect(screen.getByRole('button', { name: 'Save' })).toBeTruthy()
  })

  it('offers nothing to change to someone who cannot', async () => {
    getReadMode.mockResolvedValue({ source: 'custom', profile: PROFILE })
    listConnectedSites.mockResolvedValue(SITES([ROW()], ['other']))
    show(false)
    await screen.findByTestId('connected-site')
    expect(screen.queryByRole('button', { name: 'Change' })).toBeNull()
    expect(screen.queryByRole('switch')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Add site' })).toBeNull()
  })
})

describe('connected sites', () => {
  it('shows a row with its tools, its login and its internal name', async () => {
    listConnectedSites.mockResolvedValue(SITES([ROW()]))
    show()
    const row = await screen.findByTestId('connected-site')
    expect(row.textContent).toContain('connect-labs')
    expect(row.textContent).toContain('internal name: connect')
    expect(row.textContent).toContain('mcp__*connect_labs__*')
    expect(row.textContent).toContain('LABS_MCP_TOKEN')
  })

  it('names the canopy app in words, and says what no limit means', async () => {
    listConnectedSites.mockResolvedValue(SITES([ROW({ site: 'canopy-web', key: 'canopy', ceiling: [], own_login: [] })]))
    show()
    const row = await screen.findByTestId('connected-site')
    expect(row.textContent).toContain('canopy (this app)')
    expect(row.textContent).toContain('whatever the site allows')
  })

  it('adds only a site the workspace has and the agent does not, with no fields', async () => {
    listConnectedSites.mockResolvedValue(SITES([ROW()], ['connect-labs', 'other-site']))
    putConnectedSite.mockResolvedValue(SITES([ROW(), ROW({ site: 'other-site', key: 'other_site' })], ['connect-labs', 'other-site']))
    show()
    const picker = await screen.findByLabelText('Site to add')
    expect(within(picker).queryByRole('option', { name: 'connect-labs' })).toBeNull()
    fireEvent.change(picker, { target: { value: 'other-site' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add site' }))
    await waitFor(() => expect(screen.getAllByTestId('connected-site')).toHaveLength(2))
    expect(putConnectedSite).toHaveBeenCalledWith('hal', 'other-site', {})
  })

  it('flips sensitive data, sending only that', async () => {
    listConnectedSites.mockResolvedValue(SITES([ROW()]))
    putConnectedSite.mockResolvedValue(SITES([ROW({ sensitive: true })]))
    show()
    fireEvent.click(await screen.findByRole('switch', { name: 'Handles sensitive data on connect-labs' }))
    await screen.findByTestId('handles-sensitive')
    expect(putConnectedSite).toHaveBeenCalledWith('hal', 'connect-labs', { sensitive: true })
  })

  it('shows the structural refusal when a row cannot be sensitive', async () => {
    listConnectedSites.mockResolvedValue(SITES([ROW()]))
    putConnectedSite.mockRejectedValue(new Error('may run no shell'))
    show()
    fireEvent.click(await screen.findByRole('switch'))
    expect((await screen.findByRole('alert')).textContent).toContain('may run no shell')
  })

  it('edits a row, including its tools and login', async () => {
    listConnectedSites.mockResolvedValue(SITES([ROW()]))
    putConnectedSite.mockResolvedValue(SITES([ROW({ ceiling: [] })]))
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Change connect-labs' }))
    fireEvent.change(screen.getByLabelText(/Site tools it may use/), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save' })).toBeNull())
    expect(putConnectedSite).toHaveBeenCalledWith('hal', 'connect-labs', expect.objectContaining({ ceiling: [], own_login: ['LABS_MCP_TOKEN'] }))
  })

  it('removes a row', async () => {
    listConnectedSites.mockResolvedValue(SITES([ROW()]))
    deleteConnectedSite.mockResolvedValue(SITES())
    show()
    fireEvent.click(await screen.findByRole('button', { name: 'Remove connect-labs' }))
    await screen.findByText('Hal cannot be reached from any site.')
  })

  it('re-reads the history after a save', async () => {
    listConnectedSites.mockResolvedValue(SITES([ROW()]))
    putConnectedSite.mockResolvedValue(SITES([ROW({ sensitive: true })]))
    getAgentSettingsHistory.mockResolvedValueOnce([]).mockResolvedValue([
      { id: 1, created_at: '2026-11-05T00:00:00Z', actor_email: 'jo@dimagi.com', via: 'browser', operation: 'put_connected_site', target: 'hal / connect-labs' },
    ])
    show()
    fireEvent.click(await screen.findByRole('switch'))
    expect((await screen.findByTestId('settings-history')).textContent).toContain('Site changed')
  })
})
