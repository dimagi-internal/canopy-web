// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ConnectedApp, ConnectionTest } from '@/api/connectedApps'

const testConnectedApp = vi.fn()
vi.mock('@/api/connectedApps', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/connectedApps')>()),
  testConnectedApp: (...args: unknown[]) => testConnectedApp(...args),
}))

const { ConnectionTester, ConnectionTestTable, SiteHealthTable, liveProbeSummary } = await import(
  './ConnectedAppsPage'
)

afterEach(() => {
  cleanup()
  testConnectedApp.mockReset()
})

const APP = { id: 7, name: 'connect-labs' } as ConnectedApp

const RESULT: ConnectionTest = {
  ok: false,
  checks: [
    { name: 'jwks_reachable', label: 'JWKS URL answers', status: 'pass', detail: 'https://host.test/jwks.json' },
    { name: 'client_accepted', label: 'Accepts canopy as its client', status: 'fail',
      detail: 'invalid_client: the host does not accept this client_id' },
    { name: 'host_grants', label: 'Acts as the visitor (host grants)', status: 'skip', detail: 'not configured' },
  ],
  live_probe: [],
  live_probe_ok: null,
}

const PROBE_FAILED: ConnectionTest = {
  ok: false,
  checks: [RESULT.checks[0]],
  live_probe: [
    { name: 'probe_issued', label: 'The site issues a probe grant', status: 'pass', detail: "principal '7'" },
    { name: 'live_grant_redeemed', label: 'canopy redeems it (jwt-bearer + DPoP)', status: 'pass', detail: '' },
    { name: 'probe_tool_succeeds', label: "The probe's tool runs as the probe user", status: 'pass', detail: '' },
    { name: 'out_of_scope_refused', label: 'A tool outside the scope is refused', status: 'fail',
      detail: 'org_delete RAN with a marketplace:read token' },
  ],
  live_probe_ok: false,
}

/**
 * "Test connection" exists because every Connected-site setting fails closed and
 * silently. The table is the whole point: one row per check, the verdict, and
 * the reason — a failure with no reason is the silence it replaces.
 */
describe('the connection test table', () => {
  it('shows one row per check with its status, label and reason', () => {
    render(<ConnectionTestTable result={RESULT} />)
    const rows = screen.getAllByRole('row').slice(1)
    expect(rows).toHaveLength(3)
    expect(within(rows[1]).getByText('fail')).toBeTruthy()
    expect(within(rows[1]).getByText('Accepts canopy as its client')).toBeTruthy()
    expect(within(rows[1]).getByText(/invalid_client/)).toBeTruthy()
    expect(within(rows[2]).getByText('skip')).toBeTruthy()
    expect(screen.getByText('1 of 3 checks failed.')).toBeTruthy()
  })

  it('says so when everything passed', () => {
    render(
      <ConnectionTestTable
        result={{ ok: true, checks: [RESULT.checks[0]], live_probe: [], live_probe_ok: null }}
      />,
    )
    expect(screen.getByText('Every check passed.')).toBeTruthy()
  })
})

describe('the Test connection button', () => {
  it('runs the test for THIS site and renders the result', async () => {
    testConnectedApp.mockResolvedValue(RESULT)
    render(<ConnectionTester slug="connect" app={APP} />)
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }))
    await waitFor(() => expect(screen.getByText('1 of 3 checks failed.')).toBeTruthy())
    expect(testConnectedApp).toHaveBeenCalledWith('connect', 7)
  })

  it('shows the error instead of a table when the test could not run', async () => {
    testConnectedApp.mockRejectedValue(new Error('Only a workspace owner can do that.'))
    render(<ConnectionTester slug="connect" app={APP} />)
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }))
    await waitFor(() => expect(screen.getByText('Only a workspace owner can do that.')).toBeTruthy())
    expect(screen.queryByRole('table')).toBeNull()
  })
})

describe('the live probe section', () => {
  it('shows the probe steps under their own heading and names the failing step', () => {
    render(<ConnectionTestTable result={PROBE_FAILED} />)
    expect(screen.getByText('Live probe')).toBeTruthy()
    expect(screen.getByText('Live probe failed: A tool outside the scope is refused.')).toBeTruthy()
    expect(screen.getByText(/RAN with a marketplace:read token/)).toBeTruthy()
    // the settings checks and the probe are two tables
    expect(screen.getAllByRole('table')).toHaveLength(2)
  })

  it('is absent when the test ran no probe at all', () => {
    render(<ConnectionTestTable result={RESULT} />)
    expect(screen.queryByText('Live probe')).toBeNull()
  })

  it('says why there was no verdict', () => {
    expect(
      liveProbeSummary({
        live_probe: [{ name: 'probe_issued', label: 'x', status: 'skip', detail: 'the site has no probe identity' }],
        live_probe_ok: null,
      }),
    ).toBe('Live probe not run: the site has no probe identity.')
    expect(liveProbeSummary({ live_probe: [], live_probe_ok: true })).toMatch(/passed/)
  })
})

describe('the site health table', () => {
  const now = new Date('2026-09-28T12:00:00Z')
  const site = (over: Partial<ConnectedApp>): ConnectedApp =>
    ({
      id: 1,
      name: 'connect-labs',
      issues_host_grants: true,
      revoked: false,
      live_probe: { at: '2026-09-28T11:50:00Z', ok: true, step: '', step_label: '', reason: '' },
      traffic: { last_redeemed_at: '2026-09-28T09:00:00Z', last_site_call_at: null, refusals_24h: 0 },
      ...over,
    }) as ConnectedApp

  it('lists only sites that let an agent act as a visitor, with probe and traffic side by side', () => {
    render(
      <SiteHealthTable
        now={now}
        apps={[
          site({}),
          site({ id: 2, name: 'no-grants', issues_host_grants: false }),
          site({
            id: 3,
            name: 'ace-web',
            live_probe: { at: '2026-09-28T11:00:00Z', ok: false, step: 'dpop_required',
                          step_label: 'The token is refused without a valid DPoP proof',
                          reason: 'the site ACCEPTED the token with bearer' },
            traffic: { last_redeemed_at: null, last_site_call_at: null, refusals_24h: 4 },
          }),
        ]}
      />,
    )
    const rows = screen.getAllByRole('row').slice(1)
    expect(rows).toHaveLength(2)
    expect(within(rows[0]).getByText('pass')).toBeTruthy()
    expect(within(rows[0]).getByText('10m ago')).toBeTruthy()
    expect(within(rows[0]).getByText('3h ago')).toBeTruthy()
    expect(within(rows[1]).getByText('fail')).toBeTruthy()
    expect(within(rows[1]).getByText('The token is refused without a valid DPoP proof')).toBeTruthy()
    expect(within(rows[1]).getByText('4')).toBeTruthy()
  })

  it('renders nothing when no site acts as a visitor', () => {
    const { container } = render(<SiteHealthTable apps={[site({ issues_host_grants: false })]} />)
    expect(container.innerHTML).toBe('')
  })
})
