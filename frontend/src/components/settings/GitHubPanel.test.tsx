// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

// Mocked at the api module rather than at fetch: what is worth asserting here
// is which STATE the panel renders for a given connection, and the transport is
// covered by tests/test_github_connect.py on the server side.
vi.mock('@/api/github', () => ({
  getGitHubConnection: vi.fn(),
  disconnectGitHub: vi.fn(),
  listGitHubInstallations: vi.fn(),
  gitHubConnectPath: () => '/auth/github/start/',
}))

import { getGitHubConnection, listGitHubInstallations } from '@/api/github'
import { GitHubPanel } from './GitHubPanel'

const asMock = getGitHubConnection as unknown as ReturnType<typeof vi.fn>
const instMock = listGitHubInstallations as unknown as ReturnType<typeof vi.fn>

const ONE_INSTALL = [
  { installation_id: 1, account_login: 'jjackson', account_type: 'User', is_org: false },
]

function conn(over: Record<string, unknown> = {}) {
  return {
    configured: true,
    connected: false,
    github_login: '',
    needs_reconnect: false,
    install_url: '',
    ...over,
  }
}

function mount(search = '') {
  return render(
    <MemoryRouter initialEntries={[`/settings${search}`]}>
      <GitHubPanel />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  asMock.mockReset()
  instMock.mockReset()
  // Default: access granted somewhere. Tests that care about the
  // authorized-but-granted-nothing state override this explicitly, because an
  // empty list is a real answer rather than an absent one.
  instMock.mockResolvedValue(ONE_INSTALL)
})

afterEach(cleanup)

describe('GitHubPanel', () => {
  it('tells you GitHub is not set up rather than offering a button that cannot work', async () => {
    // The state of a fresh checkout, and of this deployment until the client
    // secret is set. A Connect button here would dead-end on GitHub's side
    // with an error about an unknown client.
    asMock.mockResolvedValue(conn({ configured: false }))
    mount()
    expect(await screen.findByText(/not set up on this deployment/i)).toBeTruthy()
    expect(screen.queryByText(/^Connect GitHub$/)).toBeNull()
  })

  it('names the repository-scope choice, because the safe option is not the obvious one', async () => {
    // Unsaid, people click "All repositories" — it reads like the default. The
    // advice sits on the button that leads to the INSTALL flow, which is the
    // only screen where this choice actually appears; the first version put it
    // on the authorize button and described a picker the user never saw.
    asMock.mockResolvedValue(conn())
    mount()
    expect(await screen.findByText(/Only select repositories/)).toBeTruthy()
  })

  it('reports authorized-but-no-repo-access instead of calling it success', async () => {
    // THE REGRESSION THIS PANEL SHIPPED WITH. Authorizing and installing are
    // separate GitHub flows and only the install one asks about repositories,
    // so `connected: true` with an empty installation list means a valid token
    // that can reach nothing. Measured on labs 2026-09-13: the panel said
    // "Connected as @jjackson" while nothing could be pushed.
    asMock.mockResolvedValue(conn({ connected: true, github_login: 'jjackson' }))
    instMock.mockResolvedValue([])
    mount()
    expect(await screen.findByText(/no repository access yet/i)).toBeTruthy()
    expect(screen.getByText(/Choose repositories/)).toBeTruthy()
    // And it must NOT read as finished.
    expect(screen.queryByRole('button', { name: /^disconnect$/i })).toBeNull()
  })

  it('names where access was granted once there is an installation', async () => {
    asMock.mockResolvedValue(conn({ connected: true, github_login: 'jjackson' }))
    instMock.mockResolvedValue([
      { installation_id: 1, account_login: 'jjackson', account_type: 'User', is_org: false },
      { installation_id: 2, account_login: 'dimagi-internal', account_type: 'Organization', is_org: true },
    ])
    mount()
    expect(await screen.findByText(/jjackson, dimagi-internal/)).toBeTruthy()
  })

  it('does not ask GitHub for installations when there is no usable grant', async () => {
    // The call needs a token to make. Firing it while disconnected would be a
    // guaranteed error surfaced as if the user had done something wrong.
    asMock.mockResolvedValue(conn({ connected: false }))
    mount()
    await screen.findByText(/Only select repositories/)
    expect(instMock).not.toHaveBeenCalled()
  })

  it('names the missing app setting when an install returns no code', async () => {
    // Unguessable from the outside: GitHub shows the app installed while
    // canopy stored nothing, because the app lacks "Request user authorization
    // (OAuth) during installation".
    asMock.mockResolvedValue(conn())
    mount('?github=install_without_code')
    expect(await screen.findByText(/Request user authorization/i)).toBeTruthy()
  })

  it('shows the connected account so you can tell it is the one you meant', async () => {
    asMock.mockResolvedValue(conn({ connected: true, github_login: 'jjackson' }))
    mount()
    expect(await screen.findByText('@jjackson')).toBeTruthy()
    expect(screen.getByRole('button', { name: /disconnect/i })).toBeTruthy()
  })

  it('offers Reconnect — not Disconnect — when the grant went stale', async () => {
    // Six months unused, revoked on GitHub, or the app gained a permission the
    // install has not re-approved. All one state to the user, and the remedy is
    // the button: offering "Disconnect" here would be the wrong action.
    asMock.mockResolvedValue(
      conn({ connected: true, github_login: 'jjackson', needs_reconnect: true }),
    )
    mount()
    expect(await screen.findByText(/expired or was revoked/i)).toBeTruthy()
    expect(screen.getByText(/Reconnect GitHub/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^disconnect$/i })).toBeNull()
    // No point asking GitHub for installations with a grant that cannot mint a
    // token — the call would fail by construction.
    expect(instMock).not.toHaveBeenCalled()
  })

  it('reports the callback outcome, which arrives as a query param not a response', async () => {
    // The OAuth flow leaves the SPA, so the result comes back as a full-page
    // load at /settings?github=… — there is no fetch whose error could surface
    // it. A failure that reached only the server log would look to the user
    // like a button that does nothing.
    asMock.mockResolvedValue(conn())
    mount('?github=error&detail=bad+verification+code')
    expect(await screen.findByText(/bad verification code/i)).toBeTruthy()
  })

  it('treats an installation-update bounce as information, not failure', async () => {
    // The "Redirect on update" setting sends people here after they add or
    // remove repositories, with no code to exchange. Reporting that as a
    // failed connection would be wrong and alarming.
    asMock.mockResolvedValue(conn({ connected: true, github_login: 'jjackson' }))
    mount('?github=installation_updated')
    expect(await screen.findByText(/installation was updated/i)).toBeTruthy()
  })

  it('omits the install link when the app slug is unknown', async () => {
    // Rather than rendering https://github.com/apps//installations/new, which
    // 404s.
    asMock.mockResolvedValue(conn({ connected: true, github_login: 'jjackson', install_url: '' }))
    mount()
    await screen.findByText('@jjackson')
    expect(screen.queryByText(/Install it there/)).toBeNull()
  })

  it('says that disconnecting is local, and links where to revoke upstream', async () => {
    // Claiming to revoke on GitHub's side and silently failing would be worse
    // than saying which half this does.
    asMock.mockResolvedValue(conn({ connected: true, github_login: 'jjackson' }))
    mount()
    expect(await screen.findByText(/removes canopy/i)).toBeTruthy()
    const link = screen.getByText(/your GitHub installations/i) as HTMLAnchorElement
    expect(link.getAttribute('href')).toBe('https://github.com/settings/installations')
  })

  it('renders nothing until the status is known, rather than flashing a wrong state', async () => {
    let resolve: (v: unknown) => void = () => {}
    asMock.mockReturnValue(new Promise((r) => { resolve = r }))
    const { container } = mount()
    expect(container.textContent).toBe('')
    resolve(conn({ configured: false }))
    await waitFor(() => expect(container.textContent).toContain('not set up'))
  })
})
