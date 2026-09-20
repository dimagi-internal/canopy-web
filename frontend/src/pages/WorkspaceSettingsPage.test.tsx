// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    workspaces: [{ slug: 'connect', display_name: 'Connect', role: 'owner' }],
    active: 'connect',
    loading: false,
    refresh: async () => {},
  }),
}))

const { SETTINGS_SECTIONS, WorkspaceSettingsPage } = await import('./WorkspaceSettingsPage')
const { SettingsRedirect } = await import('../router')

afterEach(() => cleanup())

function Where() {
  return <div data-testid="where">{useLocation().pathname + useLocation().search}</div>
}

/** The settings shell with a stand-in for whichever section is active. */
function renderSettings(at: string) {
  render(
    <MemoryRouter initialEntries={[at]}>
      <Routes>
        <Route path="/w/:workspace/settings" element={<WorkspaceSettingsPage />}>
          {SETTINGS_SECTIONS.map((s) => (
            <Route key={s.segment} path={s.segment} element={<div>{s.segment} section</div>} />
          ))}
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe('WorkspaceSettingsPage', () => {
  it('offers every section as a tab, under the workspace it belongs to', () => {
    renderSettings('/w/connect/settings/slack')
    for (const section of SETTINGS_SECTIONS) {
      expect(screen.getByRole('link', { name: section.label }).getAttribute('href')).toBe(
        `/w/connect/settings/${section.segment}`,
      )
    }
    expect(screen.getByText('slack section')).toBeTruthy()
  })

  it('points at the personal settings page too — two settings, so say which is which', () => {
    renderSettings('/w/connect/settings/members')
    expect(screen.getByRole('link', { name: 'your settings' }).getAttribute('href')).toBe('/settings')
  })
})

describe('SettingsRedirect', () => {
  // /w/:workspace/slack was handed to a human the day this fold shipped. A
  // dead link is the one outcome this whole change is not allowed to produce.
  it.each([
    ['/w/connect/slack', 'slack', '/w/connect/settings/slack'],
    ['/w/connect/members', 'members', '/w/connect/settings/members'],
    ['/w/connect/inbound', 'inbound', '/w/connect/settings/inbound'],
    ['/w/connect/connected-apps', 'connected-apps', '/w/connect/settings/connected-apps'],
  ])('sends %s to its section', (from, to, expected) => {
    render(
      <MemoryRouter initialEntries={[from]}>
        <Routes>
          <Route path={`/w/:workspace/${to}`} element={<SettingsRedirect to={to} />} />
          <Route path="/w/:workspace/settings/*" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.getByTestId('where').textContent).toBe(expected)
  })

  it('carries the query across, since a link can arrive with one', () => {
    render(
      <MemoryRouter initialEntries={['/w/connect/inbound?mailbox=ace']}>
        <Routes>
          <Route path="/w/:workspace/inbound" element={<SettingsRedirect to="inbound" />} />
          <Route path="/w/:workspace/settings/*" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.getByTestId('where').textContent).toBe('/w/connect/settings/inbound?mailbox=ace')
  })
})
