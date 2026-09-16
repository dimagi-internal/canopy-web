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
    requestContext: async () => ({ route: '/insights' }),
    runAction: async () => undefined,
    actions: (): ActionSpec[] => [],
    onActionsChanged: () => () => undefined,
    // `null`, not `{}`: a host that does not use the state channel must not be
    // read as one declaring the user's screen blank.
    pageState: () => null,
    onPageStateChanged: () => () => undefined,
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
  const box = await screen.findByPlaceholderText(/^Message /)
  fireEvent.change(box, { target: { value: text } })
  fireEvent.click(screen.getByRole('button', { name: 'Send' }))
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
    await screen.findByPlaceholderText(/^Message /)

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
      actions: () => [{ name: 'dismissInsights' }] as never,
      pageState: () => ({ visible_ids: [1, 2, 3], backing_tool: 'list_insights' }),
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
