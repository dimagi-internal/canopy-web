// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { EmbedApp } from './EmbedApp'
import type { ActionSpec, HostInit, HostLink, PanelSession } from './hostLink'

/**
 * The page opens a conversation it kept a pointer to (`widget.openSession`), and
 * hears which conversation is open (`onSession`). The panel reads the conversation
 * AS THE VISITOR — through the frame's own token — so canopy's session ACL decides
 * what they get; the page is told an id, where it began and whether it is private,
 * and never anything it said.
 */

const ID = '0f8fad5b-d9cb-469f-a165-70867728950e'
const AGENTS = [{ slug: 'labsdata', name: 'Labs data', description: '', avatar_url: '', workspace: 'connect' }]

let calls: Array<{ url: string; init?: RequestInit }>
let reports: Array<PanelSession | null>
let openListener: ((id: string) => void) | null

function fakeLink(): HostLink {
  const init: HostInit = { token: 't', agent: 'labsdata', actions: [] }
  return {
    waitForInit: async () => init,
    requestToken: async () => ({ token: 'tok', expiresAt: '' }),
    requestContext: async () => ({}),
    runAction: async () => undefined,
    actions: (): ActionSpec[] => [],
    onActionsChanged: () => () => undefined,
    pageState: () => null,
    onPageStateChanged: () => () => undefined,
    onThemeChanged: () => () => undefined,
    onAsk: () => () => undefined,
    onOpenSession: (l) => {
      openListener = l
      return () => undefined
    },
    reportSession: (s) => reports.push(s),
    invalidate: () => undefined,
    requestClose: () => undefined,
    reportUnavailable: () => undefined,
    requestHeight: () => undefined,
    requestExpand: () => undefined,
    isExpanded: () => false,
    onDisplayChanged: () => () => undefined,
    requestSnap: () => undefined,
    isSnapped: () => false,
    onSnapChanged: () => () => undefined,
    dispose: () => undefined,
  }
}

function serve(detail: { status: number; body?: Record<string, unknown> }) {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), init })
    const path = String(url)
    const reply = (status: number, body: unknown) =>
      ({ ok: status < 400, status, json: async () => body, text: async () => JSON.stringify(body) }) as Response
    if (path.includes('/api/embed/agents')) return reply(200, AGENTS)
    if (path.endsWith(`/canopy-sessions/${ID}`) && (!init?.method || init.method === 'GET')) {
      return reply(detail.status, detail.body ?? { detail: 'session not found' })
    }
    if (path.endsWith(`/canopy-sessions/${ID}/join-requests`) && init?.method === 'POST') {
      return reply(200, { id: 'r1', session_id: ID, status: 'pending' })
    }
    if (path.includes('/canopy-sessions/?')) return reply(200, [])
    return reply(200, {})
  }))
}

beforeEach(() => {
  calls = []
  reports = []
  openListener = null
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function openFromPage() {
  render(<EmbedApp link={fakeLink()} app="connect-labs" />)
  await screen.findByPlaceholderText(/^Type a message/)
  await waitFor(() => expect(openListener).not.toBeNull())
  openListener!(ID)
}

const bearer = (c?: { init?: RequestInit }) =>
  (c?.init?.headers as Record<string, string> | undefined)?.Authorization

describe('opening a conversation the page kept a pointer to', () => {
  it('reads it as the visitor, opens it, and reports the pointer — never its words', async () => {
    serve({ status: 200, body: {
      id: ID, agent_slug: 'labsdata', sensitive: false, you_are_in_it: null,
      origin_resource: 'labs-data-space://7', title: 'secret title', opening: 'secret words',
    } })
    await openFromPage()
    await waitFor(() => expect(reports.at(-1)).toEqual({ id: ID, resource: 'labs-data-space://7', private: false }))
    const read = calls.find((c) => c.url.endsWith(`/canopy-sessions/${ID}`))
    // Through the frame's own token: canopy's session ACL is what decides.
    expect(bearer(read)).toMatch(/^Bearer /)
    expect(JSON.stringify(reports)).not.toContain('secret')
  })

  it('one canopy will not give them says so, and the page is told nothing is open', async () => {
    serve({ status: 404 })
    await openFromPage()
    expect(await screen.findByTestId('session-unavailable')).toBeTruthy()
    expect(reports.every((r) => r === null)).toBe(true)
  })

  it('one with an agent this site does not offer is not opened', async () => {
    serve({ status: 200, body: { id: ID, agent_slug: 'hal', sensitive: false } })
    await openFromPage()
    expect(await screen.findByTestId('session-unavailable')).toBeTruthy()
  })

  it('a private one they are not in shows the ask-to-join interstitial, through the frame', async () => {
    serve({ status: 200, body: {
      id: ID, agent_slug: 'labsdata', sensitive: true, you_are_in_it: false,
      created_by: 3, started_by_name: 'Ada', your_join_request: '',
    } })
    await openFromPage()
    expect(await screen.findByTestId('private-interstitial')).toBeTruthy()
    fireEvent.click(screen.getByTestId('private-ask'))
    await screen.findByTestId('private-asked')
    const ask = calls.find((c) => c.url.endsWith(`/canopy-sessions/${ID}/join-requests`))
    expect(ask?.init?.method).toBe('POST')
    expect(bearer(ask)).toMatch(/^Bearer /)
    expect(reports.every((r) => r === null)).toBe(true)
  })
})
