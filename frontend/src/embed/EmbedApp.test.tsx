// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react'
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
const created = () =>
  calls.find(
    (c) =>
      c.init?.method === 'POST' &&
      (/\/canopy-sessions\/$/.test(c.url) || /\/contact\/sessions$/.test(c.url)),
  )

describe('starting a conversation', () => {
  it('creates the session in the AGENT\'s workspace, not the caller\'s default', async () => {
    // THE bug. `/api/canopy-sessions/` resolves to the caller's default
    // workspace and `create_session` 404s an agent that is not in it — so an
    // agent in a second workspace was offered by the picker and refused on
    // click. Worse for a user with no unambiguous default: a 422 before it
    // even looks at the agent.
    render(<EmbedApp link={fakeLink({ waitForInit: async () => ({ token: 't', agent: 'hal', actions: [] }) })} app="canopy-web" />)

    await waitFor(() => expect(created()).toBeDefined())
    expect(created()!.url).toContain('/api/w/dimagi/canopy-sessions/')
  })

  it('uses the workspace of whichever agent was picked', async () => {
    render(<EmbedApp link={fakeLink({ waitForInit: async () => ({ token: 't', agent: 'echo', actions: [] }) })} app="canopy-web" />)

    await waitFor(() => expect(created()).toBeDefined())
    expect(created()!.url).toContain('/api/w/connect/canopy-sessions/')
  })

  it('still refuses to send embed_app — canopy stamps it from the token', async () => {
    render(<EmbedApp link={fakeLink({ waitForInit: async () => ({ token: 't', agent: 'hal', actions: [] }) })} app="canopy-web" />)

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
