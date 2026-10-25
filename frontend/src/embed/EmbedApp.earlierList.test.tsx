// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { EmbedApp } from './EmbedApp'
import type { ActionSpec, HostInit, HostLink } from './hostLink'

/**
 * The start screen's list of earlier conversations, on a host page (#2063).
 *
 * Private conversations keep no words to be named by, so the list tells them
 * apart by metadata alone — when each began and on which page — and it offers
 * only conversations the visitor can open. Opening one is read as the visitor
 * first, and a conversation canopy will not open live says so, rather than a
 * chat that reads "Reconnecting…" over "Nothing here yet" forever.
 */

// An agent registered with its slug as its name, as `labsdata` is.
const AGENTS = [{ slug: 'labsdata', name: 'labsdata', description: '', avatar_url: '', workspace: 'connect' }]
const HERE = 'labs-data-space://12'

const ROWS = [
  { id: 'a-here', agent_slug: 'labsdata', sensitive: true, you_are_in_it: true, opening: '',
    origin_resource: HERE, created_at: '2026-09-26T09:14:00Z', last_activity_at: '2026-09-26T09:20:00Z' },
  { id: 'b-there', agent_slug: 'labsdata', sensitive: true, you_are_in_it: true, opening: '',
    origin_resource: 'labs-data-space://7', created_at: '2026-09-25T08:00:00Z', last_activity_at: '2026-09-26T08:00:00Z' },
  { id: 'c-theirs', agent_slug: 'labsdata', sensitive: true, you_are_in_it: false, opening: '',
    origin_resource: HERE, created_at: '2026-09-26T10:00:00Z', last_activity_at: '2026-09-26T10:00:00Z' },
]

let calls: Array<{ url: string; init?: RequestInit }>
let details: Record<string, Record<string, unknown>>

function fakeLink(): HostLink {
  const init: HostInit = { token: 't', agent: 'labsdata', actions: [] }
  return {
    waitForInit: async () => init,
    requestToken: async () => ({ token: 'tok', expiresAt: '' }),
    requestContext: async () => ({}),
    runAction: async () => undefined,
    actions: (): ActionSpec[] => [],
    onActionsChanged: () => () => undefined,
    pageState: () => ({ resource: HERE }),
    onPageStateChanged: () => () => undefined,
    onThemeChanged: () => () => undefined,
    onAsk: () => () => undefined,
    onOpenSession: () => () => undefined,
    reportSession: () => undefined,
    invalidate: () => undefined,
    requestClose: () => undefined,
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

/** A socket canopy turns away: it closes with 4003 as soon as it is opened. */
class RefusedSocket {
  static opened = 0
  onopen: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: ((e?: { code?: number }) => void) | null = null
  onerror: (() => void) | null = null
  readyState = 0
  constructor() {
    RefusedSocket.opened += 1
    setTimeout(() => this.onclose?.({ code: 4003 }), 0)
  }
  send() {}
  close() {}
}

beforeEach(() => {
  calls = []
  RefusedSocket.opened = 0
  details = Object.fromEntries(ROWS.map((r) => [r.id, r]))
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-09-26T12:00:00Z'))
  vi.stubGlobal('WebSocket', RefusedSocket)
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), init })
    const path = String(url)
    const reply = (status: number, body: unknown) =>
      ({ ok: status < 400, status, json: async () => body, text: async () => JSON.stringify(body) }) as Response
    if (path.includes('/api/embed/agents')) return reply(200, AGENTS)
    if (path.includes('/api/canopy-sessions/?')) return reply(200, ROWS)
    if (path.includes('/api/embed/ws-ticket')) return reply(200, { ticket: 'tk' })
    const id = Object.keys(details).find((k) => path.endsWith(`/api/canopy-sessions/${k}`))
    if (id && (!init?.method || init.method === 'GET')) return reply(200, details[id])
    return reply(200, {})
  }))
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

const open = () => render(<EmbedApp link={fakeLink()} app="connect-labs" />)

describe('the panel names its agent and its page', () => {
  it('reads an agent registered by its slug as a word, not an identifier', async () => {
    open()
    expect(await screen.findByText(/Ask Labsdata something new below/)).toBeTruthy()
    expect(screen.queryByText('labsdata')).toBeNull()
  })

  it('says which page the agent is told about', async () => {
    open()
    expect((await screen.findByTestId('asking-about')).textContent).toBe('Asking about Data space 12')
  })
})

describe('telling private conversations apart', () => {
  it('lists each by when and where it began — and only ones the visitor is in', async () => {
    open()
    await waitFor(() => expect(screen.getAllByTestId('conversation-facts')).toHaveLength(2))
    const facts = screen.getAllByTestId('conversation-facts').map((n) => n.textContent ?? '')
    expect(facts[0]).toMatch(/^Started .+ today · on this page$/)
    expect(facts[1]).toMatch(/^Started yesterday .+ · on Data space 7$/)
    // Someone else's private conversation is not "your earlier conversation".
    expect(document.querySelector('[data-session-id="c-theirs"]')).toBeNull()
  })

  it('names the × for what it does', async () => {
    open()
    const x = (await screen.findAllByRole('button', { name: 'Remove from this list' }))[0]
    expect(x.getAttribute('title')).toMatch(/Hide from this list/)
  })
})

describe('opening one from the list', () => {
  it('reads it as the visitor first, and one they are not in offers to ask to join', async () => {
    // The listing said they were in it; canopy, asked about this one, says not.
    details['a-here'] = { ...details['a-here'], you_are_in_it: false, started_by_name: 'Ada' }
    open()
    fireEvent.click(await screen.findByText((_, el) => el?.getAttribute('data-session-id') === 'a-here'))
    expect(await screen.findByTestId('private-interstitial')).toBeTruthy()
    expect(calls.some((c) => c.url.endsWith('/api/canopy-sessions/a-here'))).toBe(true)
    expect(RefusedSocket.opened).toBe(0)
  })

  it('one canopy will not open live says so once, instead of reconnecting forever', async () => {
    open()
    fireEvent.click(await screen.findByText((_, el) => el?.getAttribute('data-session-id') === 'a-here'))
    expect(await screen.findByTestId('session-unavailable')).toBeTruthy()
    expect(screen.getByText(/isn.t available here/)).toBeTruthy()
    expect(screen.queryByText('Nothing here yet.')).toBeNull()
    expect(RefusedSocket.opened).toBe(1)
    // A way on: back to a new conversation.
    fireEvent.click(screen.getByRole('button', { name: 'Start a new conversation' }))
    expect(await screen.findByText(/Ask Labsdata something new below/)).toBeTruthy()
  })
})
