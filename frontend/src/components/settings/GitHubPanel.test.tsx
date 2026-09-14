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

function install(over: Record<string, unknown> = {}) {
  return {
    installation_id: 1,
    account_login: 'jjackson',
    account_type: 'User',
    is_org: false,
    repository_selection: 'selected',
    repositories: ['jjackson/demo'],
    repository_count: 1,
    ...over,
  }
}

const ONE_INSTALL = [install()]

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

/**
 * Long enough to cover the panel's 1.5s re-check of an empty installation list.
 *
 * REAL timers on purpose. These two tests used `vi.useFakeTimers()` plus
 * `advanceTimersByTimeAsync`, which was flaky: the retry is scheduled from
 * inside a promise callback, so whether the timer exists yet depends on
 * microtask flush order relative to the advance — it passed alone and in most
 * full runs, and failed intermittently under load. A test that fails once in
 * five runs is worse than a slow one, and it also makes every OTHER failure in
 * the suite suspect. ~1.6s twice is the price.
 */
const WAIT = { timeout: 4000 }

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
    // Empty BOTH times — a settled answer, not the post-install lag the
    // re-check exists for.
    instMock.mockResolvedValue([])
    mount()
    // Real timers with a generous find, deliberately: see the note at the
    // bottom of this file on why fake timers were removed here.
    expect(await screen.findByText(/no repository access yet/i, undefined, WAIT)).toBeTruthy()
    expect(screen.getByText(/Choose repositories/)).toBeTruthy()
    // And it must NOT read as finished.
    expect(screen.queryByRole('button', { name: /^disconnect$/i })).toBeNull()
  })

  it('names the REPOSITORIES, not just the account', async () => {
    // The shape measured on labs 2026-09-13: one org, one repo. Reporting only
    // "access granted on dimagi-internal" reads like the whole organisation and
    // OVERSTATES the grant — which is the opposite of useful when the advice
    // above was "pick only the repos you want agents in". If canopy tells
    // people to scope carefully it has to show what they scoped to.
    asMock.mockResolvedValue(conn({ connected: true, github_login: 'jjackson' }))
    instMock.mockResolvedValue([
      install({
        installation_id: 161425953,
        account_login: 'dimagi-internal',
        account_type: 'Organization',
        is_org: true,
        repositories: ['dimagi-internal/ace'],
        repository_count: 1,
      }),
    ])
    mount()
    expect(await screen.findByText('dimagi-internal/ace')).toBeTruthy()
    expect(screen.getByText(/organisation/)).toBeTruthy()
  })

  it('flags an all-repositories grant instead of listing everything', async () => {
    // It is the one grant the connect advice exists to steer people away from,
    // so it should read as a choice worth revisiting rather than as a very
    // long list.
    asMock.mockResolvedValue(conn({ connected: true, github_login: 'jjackson' }))
    instMock.mockResolvedValue([
      install({ repository_selection: 'all', repositories: [], repository_count: 0 }),
    ])
    mount()
    expect(await screen.findByText(/All repositories/)).toBeTruthy()
    expect(screen.getByText(/Narrow it if you did not mean that/)).toBeTruthy()
  })

  it('says how many more there are when the grant spans a page', async () => {
    asMock.mockResolvedValue(conn({ connected: true, github_login: 'jjackson' }))
    instMock.mockResolvedValue([
      install({ repositories: ['a/one', 'a/two'], repository_count: 7 }),
    ])
    mount()
    expect(await screen.findByText(/and 5 more/)).toBeTruthy()
  })

  it('re-checks an empty list once before warning, because GitHub lags after an install', async () => {
    // Observed on labs 2026-09-13: the "no repository access" warning appeared
    // and then vanished on its own, because the first read after coming back
    // from the install screen did not yet list the brand-new installation.
    // Believing that momentary empty answer shows an alarming state that is
    // not true.
    asMock.mockResolvedValue(conn({ connected: true, github_login: 'jjackson' }))
    instMock.mockResolvedValueOnce([]).mockResolvedValueOnce(ONE_INSTALL)
    mount()
    // The populated second read wins, and the warning never appears. Asserting
    // the END state rather than trying to catch the intermediate one: the gap
    // between the first resolve and the retry is exactly the timing this test
    // must not depend on.
    expect(await screen.findByText('jjackson/demo', undefined, WAIT)).toBeTruthy()
    expect(screen.queryByText(/no repository access yet/i)).toBeNull()
    expect(instMock).toHaveBeenCalledTimes(2)
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
