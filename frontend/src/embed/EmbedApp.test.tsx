// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { EmbedApp } from './EmbedApp'
import type { ActionSpec, HostInit, HostLink } from './hostLink'

/**
 * What the frame asks canopy for, and where.
 *
 * These are URL-and-payload assertions rather than a rendering test, because
 * the bug they exist for is invisible in the DOM: the frame asked the right
 * question of the wrong tenant, and the only witness is the request it made.
 */

const AGENTS = [
  { slug: 'echo', name: 'Echo', description: '', avatar_url: '', workspace: 'connect' },
  { slug: 'hal', name: 'Hal', description: '', avatar_url: '', workspace: 'dimagi' },
]

function fakeLink(overrides: Partial<HostLink> = {}): HostLink {
  const init: HostInit = { token: 'tok', actions: [] }
  return {
    waitForInit: async () => init,
    requestToken: async () => ({ token: 'tok', expiresAt: '' }),
    requestContext: async () => ({ route: '/w/connect/agents/echo/inbox' }),
    runAction: async () => undefined,
    actions: (): ActionSpec[] => [],
    onActionsChanged: () => () => undefined,
    // `null`, not `{}`: a host that does not use the state channel must not be
    // read as one declaring the user's screen blank.
    pageState: () => null,
    onPageStateChanged: () => () => undefined,
    onThemeChanged: () => () => undefined,
    invalidate: () => undefined,
    requestClose: () => undefined,
    requestHeight: () => undefined,
    dispose: () => undefined,
    ...overrides,
  }
}

/** Every request the frame made, in order. */
let calls: Array<{ url: string; init?: RequestInit }>

beforeEach(() => {
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url: String(url), init })
      if (String(url).includes('/api/embed/agents')) {
        return { ok: true, status: 200, json: async () => AGENTS } as Response
      }
      return { ok: true, status: 200, json: async () => ({ id: 'sess-1' }) } as Response
    }),
  )
})

afterEach(() => {
  // UNMOUNT FIRST, and this is the fix for a real flake rather than tidiness.
  // Without it every component stays mounted for the rest of the file, and its
  // in-flight promises keep running: a previous test's session create would
  // resolve during a LATER test and fire its follow-up attach, which landed in
  // that test's freshly-reset `calls` array. Symptom, seen in CI on an
  // unrelated docs PR: "shows the picker rather than guessing" failing with a
  // captured POST to /api/canopy-sessions/sess-1/attach — a request the test
  // it was attributed to never made. This file was the only one in the repo
  // rendering components without cleanup.
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/**
 * The session-CREATE request, if one was made.
 *
 * Matches the collection endpoint specifically. Matching any POST containing
 * "canopy-sessions" also matched `POST /api/canopy-sessions/{id}/attach`, so
 * `expect(created()).toBeUndefined()` could fail on an attach — a request that
 * is not a create and that the failing test had not made. A helper named
 * `created` should not answer true for something else.
 */
/** The request that started a conversation, on EITHER surface.
 *
 *  A member's goes to `…/canopy-sessions/` and a contact's to
 *  `/api/contact/sessions`. Still anchored at the end so it cannot match a
 *  sub-resource like `…/sess-1/attach` — the property #785 added this regex
 *  for. */
/** Type a first message and send it — which is what now creates the session.
 *
 *  Nothing is created on mount any more (see the regression test below), so
 *  every assertion about the create call has to go through the composer, the
 *  same way a person does. */
async function say(text = 'hello') {
  // The start screen is the kit's SendBox now, the same composer the chat uses
  // once the session exists — so this drives it exactly as the chat is driven.
  const box = await screen.findByPlaceholderText(/^Type a message/)
  fireEvent.change(box, { target: { value: text } })
  fireEvent.click(screen.getByTestId('send'))
}

const created = () =>
  calls.find(
    (c) =>
      c.init?.method === 'POST' &&
      (/\/canopy-sessions\/$/.test(c.url) || /\/contact\/sessions$/.test(c.url)),
  )

describe('starting a conversation', () => {
  it('creates NOTHING until somebody actually says something', async () => {
    // The bug this exists for: the chrome sets the iframe `src` when it is
    // built, not when the panel is opened, so the frame boots on every page
    // view. Creating the session on mount therefore created one per page load
    // — three empty sessions turned up in a real list within an hour of the
    // widget being switched on, each with no runner, because no turn had ever
    // been enqueued.
    render(
      <EmbedApp
        link={fakeLink({ waitForInit: async () => ({ token: 't', agent: 'hal', actions: [] }) })}
        app="canopy-web"
      />,
    )

    // Wait until the frame has settled far enough to have created one.
    await screen.findByPlaceholderText(/^Type a message/)

    expect(created()).toBeUndefined()
    expect(calls.some((c) => c.init?.method === 'POST')).toBe(false)
  })

  it('sends the first message with the session it just created', async () => {
    render(
      <EmbedApp
        link={fakeLink({ waitForInit: async () => ({ token: 't', agent: 'hal', actions: [] }) })}
        app="canopy-web"
      />,
    )
    await say('what is stale here?')

    await waitFor(() =>
      expect(calls.some((c) => c.url.includes('/send'))).toBe(true),
    )
    const sent = calls.find((c) => c.url.includes('/send'))!
    expect(JSON.parse(String(sent.init!.body)).text).toContain('what is stale here?')
  })


  it('creates the session in the AGENT\'s workspace, not the caller\'s default', async () => {
    // THE bug. `/api/canopy-sessions/` resolves to the caller's default
    // workspace and `create_session` 404s an agent that is not in it — so an
    // agent in a second workspace was offered by the picker and refused on
    // click. Worse for a user with no unambiguous default: a 422 before it
    // even looks at the agent.
    render(<EmbedApp link={fakeLink({ waitForInit: async () => ({ token: 't', agent: 'hal', actions: [] }) })} app="canopy-web" />)
    await say()

    await waitFor(() => expect(created()).toBeDefined())
    expect(created()!.url).toContain('/api/w/dimagi/canopy-sessions/')
  })

  it('uses the workspace of whichever agent was picked', async () => {
    render(<EmbedApp link={fakeLink({ waitForInit: async () => ({ token: 't', agent: 'echo', actions: [] }) })} app="canopy-web" />)
    await say()

    await waitFor(() => expect(created()).toBeDefined())
    expect(created()!.url).toContain('/api/w/connect/canopy-sessions/')
  })

  it('still refuses to send embed_app — canopy stamps it from the token', async () => {
    render(<EmbedApp link={fakeLink({ waitForInit: async () => ({ token: 't', agent: 'hal', actions: [] }) })} app="canopy-web" />)
    await say()

    await waitFor(() => expect(created()).toBeDefined())
    expect(JSON.parse(String(created()!.init!.body))).not.toHaveProperty('embed_app')
  })

  it('shows the picker rather than guessing when several agents are on offer', async () => {
    render(<EmbedApp link={fakeLink()} app="canopy-web" />)

    expect(await screen.findByText('Echo')).toBeTruthy()
    expect(screen.getByText('Hal')).toBeTruthy()
    expect(created()).toBeUndefined()
  })
})

describe('a contact behind the frame', () => {
  const ME: {
    contact_id: number
    display_name: string
    app: string
    agents: { slug: string; name: string; description: string }[]
  } = {
    contact_id: 7,
    display_name: 'Amina',
    app: 'connect-labs',
    agents: [{ slug: 'echo', name: 'Echo', description: '' }],
  }

  function asContact() {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url: String(url), init })
        const path = String(url)
        if (path.includes('/api/embed/agents')) {
          // What the login middleware answers a request with no user.
          return { ok: false, status: 401, json: async () => ({}) } as Response
        }
        if (path.includes('/api/contact/me')) {
          return { ok: true, status: 200, json: async () => ME } as Response
        }
        return { ok: true, status: 200, json: async () => ({ id: 'sess-c' }) } as Response
      }),
    )
  }

  it('starts its conversation on the contact surface, not the tenant one', async () => {
    // The tenant path would 401: a contact has no workspace and no membership,
    // so `/api/w/{ws}/canopy-sessions/` is not a route they can reach.
    asContact()
    render(
      <EmbedApp
        link={fakeLink({ waitForInit: async () => ({ token: 't', agent: 'echo', actions: [] }) })}
        app="connect-labs"
      />,
    )
    await say()

    await waitFor(() => expect(created()).toBeDefined())
    expect(created()!.url).toContain('/api/contact/sessions')
    expect(created()!.url).not.toContain('/api/w/')
  })

  it('asks the embed surface first, so a user pays nothing for the probe', async () => {
    asContact()
    render(<EmbedApp link={fakeLink()} app="connect-labs" />)

    await waitFor(() => expect(calls.some((c) => c.url.includes('/api/contact/me'))).toBe(true))
    // First out is the user surface. The contact call comes after — with the
    // client's own one-shot 401 retry in between, which is why this asserts on
    // order rather than on an index.
    expect(calls[0].url).toContain('/api/embed/agents')
    const probe = calls.findIndex((c) => c.url.includes('/api/embed/agents'))
    const fallback = calls.findIndex((c) => c.url.includes('/api/contact/me'))
    expect(probe).toBeLessThan(fallback)
  })

  it('offers the agents the site vouched it may', async () => {
    // Two, because with exactly one the frame skips the picker and opens the
    // conversation — correct behaviour, and it made the one-agent version of
    // this test look broken.
    ME.agents = [
      { slug: 'echo', name: 'Echo', description: '' },
      { slug: 'hal', name: 'Hal', description: '' },
    ]
    asContact()
    render(<EmbedApp link={fakeLink()} app="connect-labs" />)

    expect(await screen.findByText('Echo')).toBeTruthy()
    expect(screen.getByText('Hal')).toBeTruthy()
  })
})

describe('what the agent is asked, and what the conversation ends up called', () => {
  it('leads with what the person typed, not the page context', async () => {
    // The runner names an emdash task from the prompt's OPENING words. Leading
    // with the context block produced tasks called
    // `c-context-from-the-page-i-am-on-8e56` for somebody who typed "tell me
    // about this page" — they could not find their own conversation, on a real
    // deployment, which is how this was found.
    render(
      <EmbedApp
        link={fakeLink({
          waitForInit: async () => ({ token: 't', agent: 'hal', actions: [] }),
          requestContext: async () => ({ surface: 'the supervisor inbox', path: '/supervisor' }),
        })}
        app="canopy-web"
      />,
    )
    await say('Tell me about this page')

    await waitFor(() => expect(calls.some((c) => c.url.includes('/send'))).toBe(true))
    const text = JSON.parse(String(calls.find((c) => c.url.includes('/send'))!.init!.body)).text

    expect(text.startsWith('Tell me about this page')).toBe(true)
    // The context still travels — it is moved, not dropped.
    expect(text).toContain('/supervisor')
  })

  it('sends the bare message when the host offers no context', async () => {
    render(
      <EmbedApp
        link={fakeLink({
          waitForInit: async () => ({ token: 't', agent: 'hal', actions: [] }),
          requestContext: async () => ({}),
        })}
        app="canopy-web"
      />,
    )
    await say('hello')

    await waitFor(() => expect(calls.some((c) => c.url.includes('/send'))).toBe(true))
    const text = JSON.parse(String(calls.find((c) => c.url.includes('/send'))!.init!.body)).text
    expect(text).toBe('hello')
  })
})

describe('the page is declared BEFORE the turn is queued', () => {
  /**
   * Found by driving the real widget on the real deployment. Observed order was
   *
   *     create → SEND → page-actions → page-state
   *
   * because both declarations lived in effects keyed on `sessionId`, which React
   * runs after the render that follows the send. So the FIRST message of a
   * conversation enqueued a turn describing a page that had not spoken yet, and
   * whether the agent could see the screen came down to whether a runner claimed
   * the turn before two more round-trips landed.
   *
   * It worked when I tried it. That is the point: the failing case is the
   * opening message — "close the ones I'm looking at" — and a slow runner hides
   * it every time a human checks by hand.
   */
  const withPage = () =>
    fakeLink({
      waitForInit: async () => ({ token: 't', agent: 'hal', actions: [] }),
      actions: () => [{ name: 'dismissItems' }] as never,
      pageState: () => ({ visible_ids: [1, 2, 3], backing_tool: 'list_fleet_tasks' }),
    })

  const order = () =>
    calls
      .map((c) => c.url)
      .filter((u) => /page-state|page-actions|\/send/.test(u))
      .map((u) => (u.includes('page-state') ? 'state' : u.includes('page-actions') ? 'actions' : 'send'))

  // `indexOf` alone is NOT enough, and getting this wrong nearly shipped: a
  // missing declaration returns -1, and -1 is less than every real index — so
  // the naive ordering assertion is satisfied by the thing being ABSENT. Both
  // tests below therefore assert PRESENCE first; verified by reverting the fix
  // and watching them go red.
  type Step = 'state' | 'actions' | 'send'
  const before = (first: Step, second: Step) => {
    const seq = order()
    expect(seq).toContain(first)
    expect(seq).toContain(second)
    expect(seq.indexOf(first)).toBeLessThan(seq.indexOf(second))
  }

  it('sends the person\'s words alone when the page has declared its state', async () => {
    // Until 2026-09-26 the declared state was pasted under the first message so
    // the agent would know to look — and every transcript showed a JSON dump
    // under the question the person typed. The agent now learns the page exists
    // from the caller envelope (canopy-web `caller_context.build()["page"]`,
    // surfaced by the canopy plugin's UserPromptSubmit hook), outside the
    // person's words; the state itself is declared before the send (below).
    render(<EmbedApp link={withPage()} app="canopy-web" />)

    await say('what am I looking at?')

    await waitFor(() => expect(order()).toContain('send'))
    const sent = calls.find((c) => c.url.includes('/send'))
    const body = JSON.parse(String(sent!.init?.body))
    expect(body.text).toBe('what am I looking at?')
  })

  it('declares the state as it is at send time, not at mount', async () => {
    // `setPageState` pushes on every change, so a filter applied after the panel
    // opened must be what the agent can read.
    let state: Record<string, unknown> = { visible_ids: [1], backing_tool: 'list_fleet_tasks' }
    const link = fakeLink({
      waitForInit: async () => ({ token: 't', agent: 'hal', actions: [] }),
      pageState: () => state,
    })
    render(<EmbedApp link={link} app="canopy-web" />)
    state = { visible_ids: [7, 8, 9], backing_tool: 'list_fleet_tasks' }

    await say('and now?')

    await waitFor(() => expect(calls.some((c) => c.url.includes('/send'))).toBe(true))
    const declared = calls.find((c) => c.url.includes('/page-state'))
    expect(declared).toBeTruthy()
    expect(String(declared!.init?.body)).toContain('7')
  })

  it('declares what is on screen before sending', async () => {
    render(<EmbedApp link={withPage()} app="canopy-web" />)

    await say('close the ones I am looking at')

    await waitFor(() => expect(order()).toContain('send'))
    before('state', 'send')
  })

  it('declares what the page can do before sending', async () => {
    render(<EmbedApp link={withPage()} app="canopy-web" />)

    await say('close the ones I am looking at')

    await waitFor(() => expect(order()).toContain('send'))
    before('actions', 'send')
  })

  it('still sends when the host declares no state at all', async () => {
    // A host that does not use the channel must not have its chat blocked on a
    // declaration it will never make.
    render(
      <EmbedApp
        link={fakeLink({ waitForInit: async () => ({ token: 't', agent: 'hal', actions: [] }) })}
        app="canopy-web"
      />,
    )

    await say('hello')

    await waitFor(() => expect(calls.some((c) => c.url.includes('/send'))).toBe(true))
    // And it does NOT declare an empty screen on that host's behalf.
    expect(calls.some((c) => c.url.includes('page-state'))).toBe(false)
  })
})

/**
 * What the panel SAYS while it works.
 *
 * The complaint these exist for: the widget mounts the same `ChatPanel` as
 * canopy's chat page and looked visibly worse, because the host wired none of
 * the panel's feedback seams. The first message was the worst of it — sent
 * over HTTP before the chat component exists, so nothing on the socket ever
 * announced it and the person who typed it watched their own question vanish
 * into an empty box.
 */
describe('feedback while the agent works', () => {
  async function startChatting() {
    render(
      <EmbedApp
        link={fakeLink({ waitForInit: async () => ({ token: 't', agent: 'hal', actions: [] }) })}
        app="canopy-web"
      />,
    )
    await say('what changed in this skill?')
    await waitFor(() => expect(created()).toBeDefined())
  }

  it('shows the question you just asked, instead of an empty panel', async () => {
    await startChatting()
    // It is not a server row yet — it becomes one only when the agent's
    // transcript ships it back, which is seconds away at best and never if
    // the runner is offline.
    await waitFor(() =>
      expect(screen.getByText('what changed in this skill?')).toBeTruthy(),
    )
  })

  it('says something is happening from the first paint', async () => {
    await startChatting()
    // No socket frame has arrived; this is the client-side half, which is the
    // only thing that can answer before the turn is even enqueued.
    await waitFor(() => expect(screen.getByTestId('pending-reply')).toBeTruthy())
  })

  it('does not echo the page-context block back at the person who typed', async () => {
    // The context rides the first message so the agent can read it. Showing
    // it in the transcript would bury a one-line question under a JSON blob.
    await startChatting()
    await waitFor(() => expect(screen.getByTestId('pending-reply')).toBeTruthy())
    expect(screen.queryByText(/agents\/echo\/inbox/)).toBeNull()
  })
})

describe('the agent and your earlier conversations', () => {
  const EARLIER = [
    { id: 'old-hal', agent_slug: 'hal', title: 'cw-old-thread', opening: 'Older question', created_at: '2026-09-20T10:00:00Z',
      last_activity_at: '2026-09-20T10:05:00Z' },
    { id: 'new-hal', agent_slug: 'hal', title: 'cw-new-thread', opening: 'Newer question', created_at: '2026-09-24T10:00:00Z',
      last_activity_at: '2026-09-24T10:05:00Z' },
    { id: 'echo-1', agent_slug: 'echo', title: 'cw-not-thread', opening: 'Not this agent', created_at: '2026-09-25T10:00:00Z',
      last_activity_at: '2026-09-25T10:05:00Z' },
  ]

  // The fixtures are dated; the start screen offers only the last week. Only
  // Date is faked, so the tests' own waits still run on real timers.
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-09-26T12:00:00Z'))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  function withHistory() {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url: String(url), init })
        const u = String(url)
        if (u.includes('/api/embed/agents')) {
          return { ok: true, status: 200, json: async () => AGENTS } as Response
        }
        if (u.includes('/api/canopy-sessions/?') && (!init?.method || init.method === 'GET')) {
          return { ok: true, status: 200, json: async () => EARLIER } as Response
        }
        return { ok: true, status: 200, json: async () => ({ id: 'sess-1' }) } as Response
      }),
    )
  }

  const hal = () =>
    fakeLink({ waitForInit: async () => ({ token: 't', agent: 'hal', actions: [] }) })

  it('lists only THIS agent’s earlier chats, newest first', async () => {
    withHistory()
    render(<EmbedApp link={hal()} app="connect-labs" />)

    const newer = await screen.findByText('Newer question')
    const older = screen.getByText('Older question')

    expect(screen.queryByText('Not this agent')).toBeNull()
    expect(newer.compareDocumentPosition(older) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('reopens an earlier chat without creating a new one, and names the agent', async () => {
    withHistory()
    render(<EmbedApp link={hal()} app="connect-labs" />)

    fireEvent.click(await screen.findByText('Newer question'))

    await waitFor(() => expect(screen.getByLabelText('All conversations')).toBeTruthy())
    expect(created()).toBeUndefined()
    expect(screen.getByText('Hal')).toBeTruthy()
    expect(screen.queryByText('Canopy')).toBeNull()
  })

  it('goes back to the list from a conversation', async () => {
    withHistory()
    render(<EmbedApp link={hal()} app="connect-labs" />)
    fireEvent.click(await screen.findByText('Newer question'))

    fireEvent.click(await screen.findByLabelText('All conversations'))

    expect(await screen.findByText('Older question')).toBeTruthy()
  })

  it('names an earlier chat by what was asked and when, never by canopy’s title', async () => {
    withHistory()
    render(<EmbedApp link={hal()} app="connect-labs" />)

    expect(await screen.findByText('Newer question')).toBeTruthy()
    expect(screen.queryByText(/-thread$/)).toBeNull()
    expect(screen.getAllByText(/Sep 2\d/).length).toBeGreaterThan(0)
  })

  it('leaves out a chat with nothing asked in it', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url: String(url), init })
        const u = String(url)
        if (u.includes('/api/embed/agents')) {
          return { ok: true, status: 200, json: async () => AGENTS } as Response
        }
        if (u.includes('/api/canopy-sessions/?')) {
          return {
            ok: true, status: 200,
            json: async () => [
              ...EARLIER,
              { id: 'empty-hal', agent_slug: 'hal', title: '', opening: '', created_at: '2026-09-25T11:00:00Z' },
            ],
          } as Response
        }
        return { ok: true, status: 200, json: async () => ({ id: 'sess-1' }) } as Response
      }),
    )
    render(<EmbedApp link={hal()} app="connect-labs" />)

    expect(await screen.findByText('Newer question')).toBeTruthy()
    expect(screen.queryByText('Conversation')).toBeNull()
  })

  it('asks only for conversations nobody put away', async () => {
    withHistory()
    render(<EmbedApp link={hal()} app="connect-labs" />)
    await screen.findByText('Newer question')

    const list = calls.find((c) => c.url.includes('/api/canopy-sessions/?'))
    expect(list?.url).toContain('state=active')
  })

  it('offers only the last week', async () => {
    vi.setSystemTime(new Date('2026-09-30T12:00:00Z'))
    withHistory()
    render(<EmbedApp link={hal()} app="connect-labs" />)

    expect(await screen.findByText('Newer question')).toBeTruthy()
    expect(screen.queryByText('Older question')).toBeNull()
  })

  it('× archives an earlier chat and takes it off the list', async () => {
    withHistory()
    render(<EmbedApp link={hal()} app="connect-labs" />)
    await screen.findByText('Newer question')

    fireEvent.click(screen.getAllByRole('button', { name: 'Remove from this list' })[0])

    await waitFor(() => expect(screen.queryByText('Newer question')).toBeNull())
    expect(screen.getByText('Older question')).toBeTruthy()
    const archive = calls.find((c) => c.url.endsWith('/api/canopy-sessions/new-hal/archive'))
    expect(archive?.init?.method).toBe('POST')
    expect(created()).toBeUndefined()
  })

  it('starts a new chat from inside a conversation, in words not a chevron', async () => {
    withHistory()
    render(<EmbedApp link={hal()} app="connect-labs" />)
    fireEvent.click(await screen.findByText('Newer question'))

    fireEvent.click(await screen.findByRole('button', { name: 'New chat' }))

    expect(await screen.findByText(/Ask Hal something new below/)).toBeTruthy()
    expect(created()).toBeUndefined()
  })
})

describe('agent memory in the conversation header', () => {
  const MEMORY = {
    session_id: 'sess-1',
    record: { available: true, default: true, override: null, effective: true, granted: true },
    use: { available: true, default: false, override: null, effective: false },
  }

  function serve(personStatus: number) {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url: String(url), init })
        const path = String(url)
        if (path.includes('/api/embed/agents')) {
          return { ok: true, status: 200, json: async () => AGENTS } as Response
        }
        if (path.includes('/api/people/me/sessions/')) {
          return personStatus === 200
            ? ({ ok: true, status: 200, json: async () => MEMORY } as Response)
            : ({ ok: false, status: personStatus, json: async () => ({}) } as Response)
        }
        if (path.includes('/api/embed/sessions/')) {
          return {
            ok: true,
            status: 200,
            json: async () => ({ ...MEMORY, manage_path: '/w/connect/chat/sess-1' }),
          } as Response
        }
        return { ok: true, status: 200, json: async () => ({ id: 'sess-1' }) } as Response
      }),
    )
  }

  const open = () =>
    render(
      <EmbedApp
        link={fakeLink({ waitForInit: async () => ({ token: 't', agent: 'hal', actions: [] }) })}
        app="canopy-web"
      />,
    )

  it("canopy's own widget gets the real toggles", async () => {
    serve(200)
    open()
    await say()
    expect(await screen.findByRole('switch', { name: /Learn about me/ })).toBeTruthy()
    expect(screen.getByText('Use: off')).toBeTruthy()
    expect(screen.queryByText('Change in canopy')).toBeNull()
  })

  it('a site acting for the person shows the state read-only, with a link to change it', async () => {
    serve(403)
    open()
    await say()
    expect(await screen.findByText('Change in canopy')).toBeTruthy()
    expect(screen.getByText('Learn: on')).toBeTruthy()
    expect(screen.queryByRole('switch')).toBeNull()
    expect(calls.some((c) => c.url.includes('/api/embed/sessions/sess-1/agent-memory'))).toBe(true)
  })

  it('not the visitor\'s session: nothing is shown', async () => {
    serve(404)
    open()
    await say()
    await waitFor(() => expect(calls.some((c) => c.url.includes('/api/people/me/sessions/'))).toBe(true))
    expect(screen.queryByText(/Learn:/)).toBeNull()
    expect(calls.some((c) => c.url.includes('/api/embed/sessions/'))).toBe(false)
  })
})
