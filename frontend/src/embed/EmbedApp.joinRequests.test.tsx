// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ActionSpec, HostInit, HostLink } from './hostLink'

/**
 * A request to join a private conversation, in the embedded panel — where its
 * starter usually is. The panel adds them itself: it unlocks the visitor's vault
 * key from the keyset the conversation carries, opens the conversation key, wraps it
 * to the requester and posts the participant — all through the frame's own token.
 * (The crypto is proven in `vault/sealedChat.test.ts`; here, the route it takes.)
 */

const unlocked = { userId: 1, privateKey: 'pk', publicJwk: {}, scalar: new Uint8Array() }
vi.mock('@/vault/localUnlock', () => ({ unlockLocally: vi.fn(async () => unlocked) }))
vi.mock('@/vault/sealedChat', async (orig) => ({
  ...(await orig<typeof import('@/vault/sealedChat')>()),
  openConversationKey: vi.fn(async () => ({ conversation: 'c1' })),
  wrapForRecipient: vi.fn(async () => ({ alg: 'ECDH-ES', epk: {}, n: 'n', ct: 'ct', recipient_jkt: 'jkt-ada' })),
}))

const { EmbedApp } = await import('./EmbedApp')

const AGENTS = [{ slug: 'hal', name: 'Hal', description: '', avatar_url: '', workspace: 'connect' }]
const SEALED_KEY = {
  conversation: 'c1', public_jwk: {}, current: true,
  wrap: { alg: 'ECDH-ES', epk: {}, n: 'n', ct: 'ct' },
  keyset: { user_id: 1, public_jwk: {}, seals: [{ alg: '2skd' }] },
}
const ADA = {
  id: 'r1', session_id: 'sess-1', requested_by: 'ada@dimagi.com', requested_by_name: 'Ada Lovelace',
  status: 'pending', decided_by: '', decided_at: null, created_at: '2026-10-23T12:00:00Z',
}

function fakeLink(): HostLink {
  const init: HostInit = { token: 't', agent: 'hal', actions: [] }
  return {
    waitForInit: async () => init,
    requestToken: async () => ({ token: 'tok', expiresAt: '' }),
    requestContext: async () => ({ route: '/' }),
    runAction: async () => undefined,
    actions: (): ActionSpec[] => [],
    onActionsChanged: () => () => undefined,
    pageState: () => null,
    onPageStateChanged: () => () => undefined,
    onThemeChanged: () => () => undefined,
    onAsk: () => () => undefined,
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

let calls: Array<{ url: string; init?: RequestInit }>

function serve(session: Record<string, unknown>) {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), init })
    const path = String(url)
    const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body }) as Response
    if (/\/canopy-sessions\/sess-1$/.test(path) && (!init?.method || init.method === 'GET')) return ok(session)
    if (path.includes('/api/embed/agents')) return ok(AGENTS)
    if (/\/sess-1\/join-requests$/.test(path)) return ok([ADA])
    if (path.includes('/participants/recipient')) {
      return ok({ user_id: 2, public_jwk: {}, jkt: 'jkt-ada', email: 'ada@dimagi.com' })
    }
    if (/\/sess-1\/participants$/.test(path)) return ok([])
    if (path.includes('/join-requests/r1/decline')) return ok({ ...ADA, status: 'declined' })
    return ok({ id: 'sess-1' })
  }))
}

beforeEach(() => {
  calls = []
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function openConversation() {
  render(<EmbedApp link={fakeLink()} app="canopy-web" />)
  const box = await screen.findByPlaceholderText(/^Type a message/)
  fireEvent.change(box, { target: { value: 'hello' } })
  fireEvent.click(screen.getByTestId('send'))
}

const bearer = (c?: { init?: RequestInit }) =>
  (c?.init?.headers as Record<string, string> | undefined)?.Authorization

describe('a request to join, in the embedded panel', () => {
  it('shows its owner who asked, and Add them re-wraps the key to them from the panel', async () => {
    serve({ id: 'sess-1', sensitive: true, my_role: 'owner', sealed_key: SEALED_KEY })
    await openConversation()
    const row = await screen.findByTestId('join-request')
    expect(row.textContent).toContain('Ada Lovelace asked to join this conversation')
    fireEvent.click(screen.getByTestId('join-add-r1'))
    await waitFor(() => expect(screen.queryByTestId('join-request')).toBeNull())
    const add = calls.find((c) => /\/sess-1\/participants$/.test(c.url) && c.init?.method === 'POST')
    expect(add).toBeTruthy()
    expect(JSON.parse(String(add!.init!.body))).toEqual({
      email: 'ada@dimagi.com', role: 'editor',
      wrap: { alg: 'ECDH-ES', epk: {}, n: 'n', ct: 'ct', recipient_jkt: 'jkt-ada' },
    })
    // Through the frame's own token, never the app's cookie.
    expect(bearer(add)).toMatch(/^Bearer /)
    expect(calls.some((c) => c.url.includes('/participants/recipient?email=ada%40dimagi.com'))).toBe(true)
  })

  it('Decline answers it from the panel', async () => {
    serve({ id: 'sess-1', sensitive: true, my_role: 'owner', sealed_key: SEALED_KEY })
    await openConversation()
    fireEvent.click(await screen.findByTestId('join-decline-r1'))
    await waitFor(() => expect(screen.queryByTestId('join-request')).toBeNull())
    const decline = calls.find((c) => c.url.includes('/join-requests/r1/decline'))
    expect(decline?.init?.method).toBe('POST')
    expect(bearer(decline)).toMatch(/^Bearer /)
  })

  it('an ordinary conversation asks for no requests at all', async () => {
    serve({ id: 'sess-1', sensitive: false, my_role: 'owner', sealed_key: null })
    await openConversation()
    await waitFor(() => expect(calls.some((c) => /\/canopy-sessions\/sess-1$/.test(c.url))).toBe(true))
    expect(screen.queryByTestId('join-requests')).toBeNull()
    expect(calls.some((c) => c.url.includes('/join-requests'))).toBe(false)
  })
})
