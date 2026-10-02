// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

vi.mock('@/api/turns', () => ({
  listTurnEvents: vi.fn(async () => [{ seq: 1, ts: '2026-10-01T18:06:15Z', kind: 'claimed' }]),
}))

const { TurnCard } = await import('./cards')

afterEach(cleanup)

const turn = {
  id: 't1', agent_slug: 'ace', cli_session_id: '', title: '', summary: '',
  task_ext_ids: [], work_product_urls: [], session_slug: '', share_token: '',
  started_at: '2026-10-01T18:06:15Z', ended_at: '2026-10-01T18:06:21Z', source: '',
  created_at: '2026-10-01T18:06:12Z', status: 'done', origin: 'api',
  emdash_task_id: 'c-fix-brief-61bb', reported_at: null,
  prompt: 'Fix the brief from Ada\nwith the details below', result_note: 'Opened PR #12',
  origin_ref: {},
}

describe('TurnCard', () => {
  it('shows an unreported turn by its prompt, status and result', () => {
    render(<TurnCard turn={turn} />)
    expect(screen.getByText('Fix the brief from Ada')).toBeTruthy()
    expect(screen.getByText('done')).toBeTruthy()
    expect(screen.getByText('Opened PR #12')).toBeTruthy()
    expect(screen.getByText(/no report/)).toBeTruthy()
  })

  it('opens to the full prompt and the event ledger', async () => {
    render(<TurnCard turn={turn} />)
    const toggle = screen.getByRole('button', { expanded: false })
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByText(/with the details below/)).toBeTruthy()
    expect(await screen.findByText('claimed')).toBeTruthy()
  })
})
