// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/components/alerts/AlertList', () => ({ AlertList: () => null }))
vi.mock('@/components/supervisor/RunnerDrills', () => ({ RunnerDrills: () => null }))
vi.mock('@/components/supervisor/RunnerHealth', () => ({ RunnerHealth: () => null }))

const { RunnerFacts } = await import('./RunnerOverview')

afterEach(() => cleanup())

const base = { id: 'r1', name: 'box', kind: 'cloud', status: 'online', ready: true, ready_note: '' }

describe('RunnerFacts agent CLIs', () => {
  it('reads a box that reports none as Claude Code', () => {
    render(<RunnerFacts runner={{ ...base, capabilities: { profiles: 3, envelope: 2 } } as never} />)
    expect(screen.getByTestId('runner-clis').textContent).toBe('Claude Code (signed in)')
  })

  it('lists a reported Codex that cannot confine yet', () => {
    const capabilities = { clis: {
      'claude-code': { signed_in: true, profiles: 3, envelope: 2 },
      codex: { signed_in: true, profiles: 0, envelope: 0, version: '0.164.0' },
    } }
    render(<RunnerFacts runner={{ ...base, capabilities } as never} />)
    expect(screen.getByTestId('runner-clis').textContent).toBe(
      'Claude Code (signed in) · Codex 0.164.0 (signed in, cannot confine a caller’s turn yet)')
  })
})
