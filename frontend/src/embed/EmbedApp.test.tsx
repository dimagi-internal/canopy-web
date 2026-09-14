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
const created = () =>
  calls.find((c) => c.init?.method === 'POST' && /\/canopy-sessions\/$/.test(c.url))

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
