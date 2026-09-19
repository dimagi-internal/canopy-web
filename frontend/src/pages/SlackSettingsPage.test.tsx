// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as slack from '@/api/slack'

const base = {
  workspace: 'connect', connected: true, team_name: 'Dimagi', installed_by_email: 'jj@dimagi.com',
  installed_at: '', install_url: 'https://canopy.test/canopy/auth/slack/install/?workspace=connect',
  commands: { managed: false, app_id: 'A1', set_by_email: '', synced_at: '', error: '' },
}

vi.mock('@/api/slack', async (orig) => ({
  ...(await orig<typeof import('@/api/slack')>()),
  getSlackConfig: vi.fn(async () => base),
  setSlackConfigToken: vi.fn(async () => ({ status: 'synced', detail: '', added: ['/hal'], removed: [], unfit: [] })),
}))

const { SlackSettingsPage } = await import('./SlackSettingsPage')

afterEach(() => cleanup())

function renderPage() {
  render(
    <MemoryRouter initialEntries={['/w/connect/slack']}>
      <Routes><Route path="/w/:workspace/slack" element={<SlackSettingsPage />} /></Routes>
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
  it('reads like a sentence', () => {
    expect(slack.syncSummary({ status: 'synced', detail: '', added: [], removed: [], unfit: [] }))
      .toBe('Slash commands already match.')
    expect(slack.syncSummary({ status: 'not_configured', detail: 'no token', added: [], removed: [], unfit: [] }))
      .toBe('no token')
  })
})
