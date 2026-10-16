// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as slack from '@/api/slack'

const base = {
  workspace: 'connect', connected: true, team_name: 'Dimagi', installed_by_email: 'jj@dimagi.com',
  installed_at: '', install_url: 'https://canopy.test/canopy/auth/slack/install/?workspace=connect',
  commands: { managed: false, app_id: 'A1', set_by_email: '', synced_at: '', error: '' },
  agent: { declared: false, declared_at: '' },
  history: { enabled: true, max_minutes: 120 },
  channel_ingest: { enabled: false, allowed: [] },
}

vi.mock('@/api/slack', async (orig) => ({
  ...(await orig<typeof import('@/api/slack')>()),
  getSlackConfig: vi.fn(async () => base),
  setSlackConfigToken: vi.fn(async () => ({ status: 'synced', detail: '', added: ['/hal'], removed: [], unfit: [], scopes_added: [], webhooks_moved: [], redirects_removed: [] })),
  setSlackHistory: vi.fn(async (_ws: string, enabled: boolean, max_minutes: number) => ({
    ...base, history: { enabled, max_minutes },
  })),
  setSlackChannelIngest: vi.fn(async (_ws: string, enabled: boolean) => ({ ...base, channel_ingest: { enabled, allowed: [] } })),
  allowSlackChannel: vi.fn(async () => base),
  disallowSlackChannel: vi.fn(async () => base),
  declareSlackAgent: vi.fn(async () => ({
    status: 'declared', detail: 'Slack will draw its own working indicator once the app is re-installed.',
    changed: ['features.agent_view'], reinstall_required: true, install_url: 'https://canopy.test/i',
  })),
}))

const { SlackSettingsPage } = await import('./SlackSettingsPage')

afterEach(() => cleanup())

function renderPage() {
  render(
    <MemoryRouter initialEntries={['/w/connect/settings/slack']}>
      <Routes><Route path="/w/:workspace/settings/slack" element={<SlackSettingsPage />} /></Routes>
    </MemoryRouter>,
  )
}

describe('SlackSettingsPage', () => {
  it('takes a refresh token, clears it, and says what the sync did', async () => {
    renderPage()
    const input = await screen.findByLabelText('Slack app configuration refresh token')
    fireEvent.change(input, { target: { value: 'xoxe-1-abc' } })
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    expect((await screen.findByTestId('slack-note')).textContent).toBe('Added /hal.')
    expect(vi.mocked(slack.setSlackConfigToken)).toHaveBeenCalledWith('connect', 'xoxe-1-abc')
    expect((input as HTMLInputElement).value).toBe('')
  })
})

describe('syncSummary', () => {
  it('says when the webhooks moved', () => {
    expect(slack.syncSummary({ status: 'synced', detail: '', added: [], updated: [], removed: [], unfit: [],
      scopes_added: [], webhooks_moved: ['event_subscriptions', 'interactivity'], redirects_removed: [] }))
      .toBe('Moved the event_subscriptions and interactivity URL to this address.')
  })
  it('reads like a sentence', () => {
    expect(slack.syncSummary({ status: 'synced', detail: '', added: [], updated: [], removed: [], unfit: [], scopes_added: [], webhooks_moved: [], redirects_removed: [] }))
      .toBe('Slash commands already match.')
    expect(slack.syncSummary({ status: 'not_configured', detail: 'no token', added: [], updated: [], removed: [], unfit: [], scopes_added: [], webhooks_moved: [], redirects_removed: [] }))
      .toBe('no token')
    expect(slack.syncSummary({ status: 'synced', detail: '', added: [], updated: [], removed: [], unfit: [],
      scopes_added: ['files:read'], webhooks_moved: [], redirects_removed: [] }))
      .toBe('Added the files:read permission — reconnect Slack to grant it.')
    expect(slack.syncSummary({ status: 'synced', detail: '', added: [], updated: ['/hal'], removed: [],
      unfit: [], scopes_added: [], webhooks_moved: [], redirects_removed: [] }))
      .toBe('Updated /hal.')
  })
})


describe('SlackSettingsPage — working indicator', () => {
  afterEach(cleanup)

  // Declaring edits the app's manifest, so it needs the configuration token —
  // the button stays disabled without one.
  const managed = { ...base, commands: { ...base.commands, managed: true } }
  const renderManaged = () => {
    vi.mocked(slack.getSlackConfig).mockResolvedValue(managed)
    return render(
      <MemoryRouter initialEntries={['/w/connect/settings/slack']}>
        <Routes><Route path="/w/:workspace/settings/slack" element={<SlackSettingsPage />} /></Routes>
      </MemoryRouter>,
    )
  }

  it('declares the app an agent, after confirming, and says what is left to do', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderManaged()
    const button = await screen.findByRole('button', { name: 'Declare as agent' })
    fireEvent.click(button)
    expect(confirm).toHaveBeenCalled()                       // one-way, so never silently
    expect((await screen.findByTestId('slack-note')).textContent).toContain('re-installed')
    expect(slack.declareSlackAgent).toHaveBeenCalledWith('connect')
  })

  it('does nothing if the confirm is declined', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    vi.mocked(slack.declareSlackAgent).mockClear()
    renderManaged()
    fireEvent.click(await screen.findByRole('button', { name: 'Declare as agent' }))
    expect(slack.declareSlackAgent).not.toHaveBeenCalled()
  })
})

describe('SlackSettingsPage — channel history', () => {
  afterEach(cleanup)

  it('shows the workspace policy and saves a new limit', async () => {
    vi.mocked(slack.getSlackConfig).mockResolvedValue(base)
    renderPage()
    const allow = (await screen.findByLabelText('Allow history read')) as HTMLInputElement
    const minutes = screen.getByLabelText('How far back, in minutes') as HTMLInputElement
    expect(allow.checked).toBe(true)
    expect(minutes.value).toBe('120')
    const save = screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement
    expect(save.disabled).toBe(true)                       // nothing changed yet
    fireEvent.change(minutes, { target: { value: '240' } })
    fireEvent.click(save)
    expect((await screen.findByTestId('slack-note')).textContent).toBe(
      'History reads allowed, up to 240 minutes back.')
    expect(slack.setSlackHistory).toHaveBeenCalledWith('connect', true, 240)
  })

  it('turns history reads off', async () => {
    vi.mocked(slack.getSlackConfig).mockResolvedValue(base)
    renderPage()
    fireEvent.click(await screen.findByLabelText('Allow history read'))
    expect((screen.getByLabelText('How far back, in minutes') as HTMLInputElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect((await screen.findByTestId('slack-note')).textContent).toBe('History reads are off.')
    expect(slack.setSlackHistory).toHaveBeenLastCalledWith('connect', false, 120)
  })

  it('will not save a limit outside a day', async () => {
    vi.mocked(slack.getSlackConfig).mockResolvedValue(base)
    renderPage()
    fireEvent.change(await screen.findByLabelText('How far back, in minutes'), { target: { value: '2000' } })
    expect((screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('SlackSettingsPage — whole-channel reads', () => {
  afterEach(cleanup)

  it('is off by default and an owner turns it on', async () => {
    vi.mocked(slack.getSlackConfig).mockResolvedValue(base)
    renderPage()
    expect((await screen.findByTestId('slack-channel-ingest')).textContent).toContain('Off — agents cannot ask.')
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    expect((await screen.findByTestId('slack-note')).textContent).toContain('each read still needs an approval')
    expect(slack.setSlackChannelIngest).toHaveBeenCalledWith('connect', true)
  })

  it('an owner allow-lists a channel for one agent, and removes it', async () => {
    const entry = {
      id: 'a1', agent: 'ada', channel: 'C1', channel_name: 'canopy-support', follow: false, note: 'champions',
      added_by_email: 'jj@dimagi.com', added_at: '',
    }
    vi.mocked(slack.getSlackConfig).mockResolvedValue({ ...base, channel_ingest: { enabled: false, allowed: [entry] } })
    renderPage()
    const list = await screen.findByTestId('slack-channel-allowlist')
    expect(list.textContent).toContain('#canopy-support for ada')
    // No follow option: there is nowhere to send a followed channel's new messages yet.
    expect(screen.queryByLabelText('Also follow new messages')).toBeNull()
    fireEvent.change(screen.getByLabelText('Channel'), { target: { value: '#ai-champions' } })
    fireEvent.change(screen.getByLabelText('Agent'), { target: { value: 'ada' } })
    fireEvent.click(screen.getByRole('button', { name: 'Allow-list' }))
    expect((await screen.findByTestId('slack-note')).textContent).toContain('Allow-listed #ai-champions for ada.')
    expect(slack.allowSlackChannel).toHaveBeenCalledWith('connect', {
      agent: 'ada', channel: '#ai-champions', follow: false, note: '',
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Remove #canopy-support for ada' }))
    expect((await screen.findByTestId('slack-note')).textContent).toContain('its reads were revoked')
    expect(slack.disallowSlackChannel).toHaveBeenCalledWith('connect', 'a1')
  })
})
