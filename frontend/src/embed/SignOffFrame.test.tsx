// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HostLink } from './hostLink'
import { SignOffFrame } from './SignOffFrame'

vi.mock('../auth/webauthn', () => ({
  toRequestOptions: (o: unknown) => o,
  credentialToJSON: () => ({ id: 'cred' }),
}))

const ID = 'so_abcdefghijklmnop'
const ROW = {
  id: ID, site: 'connect-labs', workspace: 'connect', subject: 'mo@dimagi.com',
  action_key: 'labs.mcp_token.full', action_title: 'Create a full-access labs MCP token', action_detail: '',
  approvers: 'subject', agent_slug: null, agent_name: null, state: 'open',
  created_at: '2026-10-28T00:00:00Z', expires_at: '2026-10-28T00:15:00Z',
}

function link(overrides: Partial<HostLink> = {}): HostLink {
  return {
    waitForInit: async () => ({ token: 'frame-tok', expiresAt: '', actions: [] }),
    requestToken: async () => ({ token: 'frame-tok', expiresAt: '' }),
    requestHeight: vi.fn(),
    requestClose: vi.fn(),
    reportSignOff: vi.fn(),
    ...overrides,
  } as unknown as HostLink
}

function respond(routes: Record<string, unknown>) {
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const path = new URL(url, 'https://canopy.example').pathname
    calls.push(`${init?.method ?? 'GET'} ${path}`)
    const body = routes[`${init?.method ?? 'GET'} ${path}`]
    return new Response(JSON.stringify(body ?? {}), { status: body === undefined ? 404 : 200,
                                                        headers: { 'Content-Type': 'application/json' } })
  }))
  return calls
}

beforeEach(() => {
  Object.defineProperty(navigator, 'credentials', { value: { get: vi.fn(async () => ({ id: 'cred' })) }, configurable: true })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('SignOffFrame', () => {
  it('shows what the site asks and approves with the passkey, telling the page only the state', async () => {
    const base = `/api/embed/sign-offs/${ID}`
    const calls = respond({
      [`GET ${base}`]: ROW,
      [`POST ${base}/approve/begin`]: { options: { challenge: 'c' } },
      [`POST ${base}/approve/finish`]: { ...ROW, state: 'approved' },
    })
    const host = link()
    render(<SignOffFrame link={host} app="connect-labs" signOffId={ID} />)
    expect((await screen.findByTestId('sign-off-title')).textContent).toContain('Create a full-access labs MCP token')
    expect(screen.getByTestId('sign-off-for').textContent).toContain('mo@dimagi.com')

    fireEvent.click(screen.getByRole('button', { name: 'Approve with passkey' }))
    await waitFor(() => expect(host.reportSignOff).toHaveBeenCalledWith(ID, 'approved'))
    expect(navigator.credentials.get).toHaveBeenCalled()
    expect(calls).toEqual(expect.arrayContaining([`POST ${base}/approve/begin`, `POST ${base}/approve/finish`]))
  })

  it('declines', async () => {
    const base = `/api/embed/sign-offs/${ID}`
    respond({ [`GET ${base}`]: ROW, [`POST ${base}/decline`]: { ...ROW, state: 'declined' } })
    const host = link()
    render(<SignOffFrame link={host} app="connect-labs" signOffId={ID} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Decline' }))
    await waitFor(() => expect(host.reportSignOff).toHaveBeenCalledWith(ID, 'declined'))
  })

  it('says so when there is nothing here for this person, and closes undecided', async () => {
    respond({})
    const host = link()
    render(<SignOffFrame link={host} app="connect-labs" signOffId={ID} />)
    expect((await screen.findByRole('alert')).textContent).toContain('no request here for you')
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(host.requestClose).toHaveBeenCalled()
    expect(host.reportSignOff).not.toHaveBeenCalled()
  })
})
