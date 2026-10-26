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
  const cli = (label: string, signed_in: boolean, confines: boolean, extra = {}) =>
    ({ label, signed_in, confines, version: '', known: true, ...extra })

  it('shows what the server reads off the box', () => {
    render(<RunnerFacts runner={{ ...base, capabilities: {}, agent_clis: { 'claude-code': cli('Claude Code', true, true) } } as never} />)
    expect(screen.getByTestId('runner-clis').textContent).toBe('Claude Code (signed in)')
  })

  it('lists a reported Codex that cannot confine yet', () => {
    const agent_clis = {
      'claude-code': cli('Claude Code', true, true),
      codex: cli('Codex', true, false, { version: '0.164.0' }),
    }
    render(<RunnerFacts runner={{ ...base, capabilities: {}, agent_clis } as never} />)
    expect(screen.getByTestId('runner-clis').textContent).toBe(
      'Claude Code (signed in) · Codex 0.164.0 (signed in, cannot confine a caller’s turn yet)')
  })

  it('says when the box names a CLI this server does not know', () => {
    const agent_clis = { gemini: cli('gemini', false, false, { known: false }) }
    render(<RunnerFacts runner={{ ...base, capabilities: {}, agent_clis } as never} />)
    expect(screen.getByTestId('runner-clis').textContent).toBe('gemini (not signed in, not known to this server)')
  })
})
