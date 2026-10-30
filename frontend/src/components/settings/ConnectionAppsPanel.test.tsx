// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ApiError } from '@/api/problem'

const m = vi.hoisted(() => ({
  listConnectionApps: vi.fn(),
  configureConnectionApp: vi.fn(),
  removeConnectionApp: vi.fn(),
  role: { current: 'admin' as string | undefined },
}))
vi.mock('@/api/connections', async (orig) => ({
  ...(await orig<typeof import('@/api/connections')>()),
  listConnectionApps: m.listConnectionApps,
  configureConnectionApp: m.configureConnectionApp,
  removeConnectionApp: m.removeConnectionApp,
}))
vi.mock('@/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({ workspaces: [{ slug: 'w', display_name: 'W', role: m.role.current }], loading: false }),
}))

import { ConnectionAppsPanel } from './ConnectionAppsPanel'

const GMAIL_READ = 'https://www.googleapis.com/auth/gmail.readonly'
const CAL_READ = 'https://www.googleapis.com/auth/calendar.readonly'

const SALESFORCE = {
  provider: 'salesforce', title: 'Salesforce', configured: true, client_id: 'sf-client', has_secret: true,
  options: { login_url: 'https://dimagi.my.salesforce.com' }, scopes: [], scope_choices: [],
  option_fields: [
    { name: 'login_url', label: 'Login URL', kind: 'url', required: true, default: null },
    { name: 'access_token_seconds', label: 'Access token lifetime (seconds)', kind: 'int', required: false, default: 1800 },
  ],
  callback_url: 'https://canopy.example/auth/connections/salesforce/w/callback/',
  connection_count: 3, updated_at: '2026-10-29T00:00:00Z', configured_by_email: 'jj@dimagi.com',
}
const GOOGLE = {
  provider: 'google', title: 'Google', configured: false, client_id: '', has_secret: false,
  options: {}, scopes: [GMAIL_READ],
  scope_choices: [{ scope: GMAIL_READ, label: 'Read mail' }, { scope: CAL_READ, label: 'Read calendars' }],
  option_fields: [{ name: 'allowed_domains', label: 'Allowed domains', kind: 'text', required: false, default: null }],
  callback_url: 'https://canopy.example/auth/connections/google/w/callback/',
  connection_count: 0, updated_at: null, configured_by_email: '',
}

const show = () => render(<ConnectionAppsPanel workspace="w" />)

beforeEach(() => {
  vi.clearAllMocks()
  m.role.current = 'admin'
  m.listConnectionApps.mockResolvedValue([SALESFORCE, GOOGLE])
  m.configureConnectionApp.mockImplementation(async (_w: string, p: string) => (p === 'google' ? GOOGLE : SALESFORCE))
  m.removeConnectionApp.mockResolvedValue(undefined)
})
afterEach(cleanup)

describe('ConnectionAppsPanel', () => {
  it('renders each provider from the API: status, callback URL and where to register it, fields', async () => {
    show()
    const sf = await screen.findByTestId('connection-app-salesforce')
    expect(m.listConnectionApps).toHaveBeenCalledWith('w')
    expect(sf.textContent).toContain('Set up')
    expect(sf.textContent).toContain(SALESFORCE.callback_url)
    expect(sf.textContent).toContain('Register this as the redirect URI in your Salesforce External Client App')
    expect((screen.getByLabelText('Client ID', { selector: '#connection-app-salesforce-client-id' }) as HTMLInputElement).value)
      .toBe('sf-client')
    expect((screen.getByLabelText('Login URL') as HTMLInputElement).value).toBe('https://dimagi.my.salesforce.com')
    // An unset option shows its default as the placeholder, not as a value.
    const ttl = screen.getByLabelText(/Access token lifetime/) as HTMLInputElement
    expect(ttl.value).toBe('')
    expect(ttl.placeholder).toBe('1800')

    const g = screen.getByTestId('connection-app-google')
    expect(g.textContent).toContain('Not set up')
    expect(g.textContent).toContain('Register this as the redirect URI in your Google OAuth client (Web application)')
    expect(g.textContent).toContain('An app left in Testing loses its connections after 7 days.')
    expect((screen.getByLabelText('Read mail') as HTMLInputElement).checked).toBe(true)
    expect((screen.getByLabelText('Read calendars') as HTMLInputElement).checked).toBe(false)
  })

  it('never renders a secret: the field starts empty, says one is saved, and clears after saving', async () => {
    show()
    await screen.findByTestId('connection-app-salesforce')
    const secret = document.getElementById('connection-app-salesforce-client-secret') as HTMLInputElement
    expect(secret.type).toBe('password')
    expect(secret.value).toBe('')
    expect(secret.placeholder).toBe('Saved — leave blank to keep')
    expect((document.getElementById('connection-app-google-client-secret') as HTMLInputElement).placeholder).toBe('')

    fireEvent.change(secret, { target: { value: 'sekrit-123' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save Salesforce' }))
    await waitFor(() => expect(m.configureConnectionApp).toHaveBeenCalled())
    expect(m.configureConnectionApp.mock.calls[0][2].client_secret).toBe('sekrit-123')
    await screen.findByText('Saved.')
    expect(secret.value).toBe('')
    expect(document.body.innerHTML).not.toContain('sekrit-123')
  })

  it('sends the full scope list on save, plus the options', async () => {
    show()
    await screen.findByTestId('connection-app-google')
    fireEvent.change(document.getElementById('connection-app-google-client-id') as HTMLInputElement, {
      target: { value: 'g-client' },
    })
    fireEvent.change(screen.getByLabelText(/Allowed domains/), { target: { value: 'dimagi.com' } })
    fireEvent.click(screen.getByLabelText('Read calendars'))
    fireEvent.click(screen.getByRole('button', { name: 'Save Google' }))
    await waitFor(() => expect(m.configureConnectionApp).toHaveBeenCalledWith('w', 'google', {
      client_id: 'g-client',
      client_secret: '',
      options: { allowed_domains: 'dimagi.com' },
      scopes: [GMAIL_READ, CAL_READ],
    }))
  })

  it('unticking a scope drops it from the list sent', async () => {
    show()
    await screen.findByTestId('connection-app-google')
    fireEvent.change(document.getElementById('connection-app-google-client-id') as HTMLInputElement, {
      target: { value: 'g-client' },
    })
    fireEvent.click(screen.getByLabelText('Read mail'))
    fireEvent.click(screen.getByRole('button', { name: 'Save Google' }))
    await waitFor(() => expect(m.configureConnectionApp).toHaveBeenCalled())
    expect(m.configureConnectionApp.mock.calls[0][2].scopes).toEqual([])
  })

  it('says what to do when the app is locked by its connections', async () => {
    m.configureConnectionApp.mockRejectedValue(new ApiError(409, 'App in use', { error: 'app_locked', detail: 'x' }))
    show()
    await screen.findByTestId('connection-app-salesforce')
    fireEvent.change(document.getElementById('connection-app-salesforce-client-id') as HTMLInputElement, {
      target: { value: 'other-client' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save Salesforce' }))
    expect(await screen.findByText(
      'Disconnect its 3 connections (Remove) before changing the client ID or login URL.',
    )).toBeTruthy()
  })

  it('confirms Remove with the number of connections it disconnects, and does nothing when declined', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    show()
    await screen.findByTestId('connection-app-salesforce')
    fireEvent.click(screen.getByRole('button', { name: 'Remove Salesforce' }))
    expect(confirm).toHaveBeenCalledWith('Remove the Salesforce app? This disconnects 3 connections.')
    expect(m.removeConnectionApp).not.toHaveBeenCalled()

    confirm.mockReturnValue(true)
    fireEvent.click(screen.getByRole('button', { name: 'Remove Salesforce' }))
    await waitFor(() => expect(m.removeConnectionApp).toHaveBeenCalledWith('w', 'salesforce'))
    confirm.mockRestore()
  })

  it('keeps "Saved." and the saved values when the save changes updated_at (no remount)', async () => {
    const after = { ...SALESFORCE, client_id: 'new-client', updated_at: '2026-10-30T12:00:00Z' }
    m.configureConnectionApp.mockResolvedValue(after)
    // Should anything re-read the list, it sees the changed row too.
    m.listConnectionApps.mockResolvedValueOnce([SALESFORCE, GOOGLE]).mockResolvedValue([after, GOOGLE])
    show()
    await screen.findByTestId('connection-app-salesforce')
    const card = screen.getByTestId('connection-app-salesforce')
    const id = document.getElementById('connection-app-salesforce-client-id') as HTMLInputElement
    fireEvent.change(id, { target: { value: 'new-client' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save Salesforce' }))
    expect(await screen.findByText('Saved.')).toBeTruthy()
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.getByText('Saved.')).toBeTruthy()
    expect(screen.getByTestId('connection-app-salesforce')).toBe(card)   // same element: not remounted
    expect((document.getElementById('connection-app-salesforce-client-id') as HTMLInputElement).value).toBe('new-client')
  })

  it('shows why a Remove failed, and keeps the app', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    m.removeConnectionApp.mockRejectedValue(new Error('Could not remove the app.'))
    show()
    await screen.findByTestId('connection-app-salesforce')
    fireEvent.click(screen.getByRole('button', { name: 'Remove Salesforce' }))
    expect(await screen.findByText('Could not remove the app.')).toBeTruthy()
    expect(m.listConnectionApps).toHaveBeenCalledTimes(1)   // nothing re-read: nothing changed
    expect(screen.getByTestId('connection-app-salesforce').textContent).toContain('Set up')
    confirm.mockRestore()
  })

  it('after a Remove, re-reads and shows the provider as not set up', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const gone = { ...SALESFORCE, configured: false, client_id: '', has_secret: false, connection_count: 0, options: {} }
    m.listConnectionApps.mockResolvedValueOnce([SALESFORCE, GOOGLE]).mockResolvedValue([gone, GOOGLE])
    show()
    await screen.findByTestId('connection-app-salesforce')
    fireEvent.click(screen.getByRole('button', { name: 'Remove Salesforce' }))
    expect(await screen.findByText('Removed.')).toBeTruthy()
    expect(screen.getByTestId('connection-app-salesforce').textContent).toContain('Not set up')
    expect((document.getElementById('connection-app-salesforce-client-id') as HTMLInputElement).value).toBe('')
    confirm.mockRestore()
  })

  it('a member who is not an admin reads one line, and nothing is fetched', async () => {
    m.role.current = 'editor'
    show()
    expect(await screen.findByText('Only a workspace admin or owner can set up connections.')).toBeTruthy()
    expect(m.listConnectionApps).not.toHaveBeenCalled()
    expect(screen.queryByTestId('connection-app-salesforce')).toBeNull()
  })

  it('option help describes the field without being part of its name', async () => {
    m.listConnectionApps.mockResolvedValue([{
      ...GOOGLE,
      option_fields: [{ ...GOOGLE.option_fields[0], help: 'Comma-separated; empty allows any account.' }],
    }])
    show()
    const input = (await screen.findByLabelText(/Allowed domains/)) as HTMLInputElement
    const help = document.getElementById(input.getAttribute('aria-describedby') ?? '')
    expect(help?.textContent).toBe('Comma-separated; empty allows any account.')
    expect(input.closest('label')?.textContent).not.toContain('Comma-separated')
  })

  it('offers no Remove for a provider that was never set up', async () => {
    m.listConnectionApps.mockResolvedValue([GOOGLE])
    show()
    await screen.findByTestId('connection-app-google')
    expect(screen.queryByRole('button', { name: /^Remove/ })).toBeNull()
  })
})
