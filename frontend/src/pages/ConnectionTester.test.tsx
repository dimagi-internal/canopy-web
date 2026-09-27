// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ConnectedApp, ConnectionTest } from '@/api/connectedApps'

const testConnectedApp = vi.fn()
vi.mock('@/api/connectedApps', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/connectedApps')>()),
  testConnectedApp: (...args: unknown[]) => testConnectedApp(...args),
}))

const { ConnectionTester, ConnectionTestTable } = await import('./ConnectedAppsPage')

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
    render(<ConnectionTestTable result={{ ok: true, checks: [RESULT.checks[0]] }} />)
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
