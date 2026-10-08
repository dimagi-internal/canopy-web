// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

const listTurnEvents = vi.fn()
vi.mock('@/api/turns', () => ({ listTurnEvents }))

const { EventLedger } = await import('./EventLedger')

afterEach(cleanup)

describe('EventLedger', () => {
  it('shows the note on an unproven_member line (canopy-web#1265)', async () => {
    listTurnEvents.mockResolvedValue([
      { seq: 1, ts: '2026-10-07T12:00:00Z', kind: 'unproven_member',
        payload: { email: 'member@example.org', note: 'treated as a contact' } },
      { seq: 2, ts: '2026-10-07T12:00:01Z', kind: 'status', payload: { status: 'running' } },
    ])
    render(<EventLedger turnId="t-1" />)
    expect(await screen.findByText(/member@example\.org: treated as a contact/)).toBeTruthy()
    expect(screen.getByText('status')).toBeTruthy()
  })
})
