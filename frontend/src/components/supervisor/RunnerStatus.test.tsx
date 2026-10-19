// @vitest-environment jsdom
//
// A paused runner had no entry in DOT and no chip, so it rendered exactly like a
// box that had died — the one state where the difference is the whole point (one
// you undo from this screen, one you go investigate).
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import type { RunnerOut } from '@/api/harness'
import { RunnerStatus } from './RunnerStatus'

function runner(overrides: Partial<RunnerOut> = {}): RunnerOut {
  return {
    id: 'r1',
    name: 'jj-mbp',
    kind: 'emdash',
    status: 'online',
    status_note: '',
    ready: true,
    ready_note: '',
    paused: false,
    paused_note: '',
    paused_at: null,
    last_heartbeat_at: '2026-07-29T20:00:00Z',
    capabilities: {},
    host: '',
    code_branch: '',
    code_version: '',
    code_sha: '',
    expected_code_sha: '',
    workspace: 'dimagi',
    owner_email: 'jjackson@dimagi.com',
    can_manage: true,
    can_administer: true,
    ...overrides,
  } as RunnerOut
}

afterEach(cleanup)

describe('RunnerStatus', () => {
  it('marks a paused runner as paused, carrying the reason', () => {
    render(<RunnerStatus runners={[runner({ paused: true, paused_note: 'token limit', status: 'paused' })]} />)
    const chip = screen.getByTestId('runner-paused-jj-mbp')
    expect(chip.textContent).toBe('paused')
    expect(chip.getAttribute('title')).toBe('token limit')
  })

  it('counts the open sessions on a paused runner — the feed does not nag about idle ones', () => {
    render(<RunnerStatus runners={[runner({ paused: true, status: 'paused', open_sessions: 4 })]} />)
    expect(screen.getByTestId('runner-paused-jj-mbp').textContent).toBe('paused · 4 open sessions')
  })

  it('shows paused instead of not-ready — the pause explains the silence', () => {
    render(<RunnerStatus runners={[runner({ paused: true, ready: false, status: 'paused' })]} />)
    expect(screen.getByTestId('runner-paused-jj-mbp')).toBeTruthy()
    expect(screen.queryByTestId('runner-notready-jj-mbp')).toBeNull()
  })

  it('still reports not-ready on a running runner', () => {
    render(<RunnerStatus runners={[runner({ ready: false })]} />)
    expect(screen.getByTestId('runner-notready-jj-mbp')).toBeTruthy()
    expect(screen.queryByTestId('runner-paused-jj-mbp')).toBeNull()
  })

  it('opens the detail view on tap', () => {
    const onSelect = vi.fn()
    render(<RunnerStatus runners={[runner()]} onSelect={onSelect} />)
    fireEvent.click(screen.getByTestId('runner-jj-mbp'))
    expect(onSelect).toHaveBeenCalled()
  })

  it('labels a directed-only box: it takes only what is put on it', () => {
    render(<RunnerStatus runners={[runner({ name: 'test-box', capabilities: { sessions: true, directed: true } })]} />)
    expect(screen.getByTestId('runner-directed-test-box').textContent).toBe('directed only')
  })

  it('shows no directed chip on an ordinary box', () => {
    render(<RunnerStatus runners={[runner()]} />)
    expect(screen.queryByTestId('runner-directed-jj-mbp')).toBeNull()
  })

  it('says plainly when a laptop serves more than one tree', () => {
    render(<RunnerStatus runners={[runner({ roots: ['dimagi', 'marshellis'], spans_trees: true })]} />)
    expect(screen.getByTestId('runner-spans-trees-jj-mbp').textContent).toBe(
      'serves dimagi, marshellis — no separation between them on this machine')
  })

  it('counts a tree the viewer cannot see without naming it', () => {
    render(<RunnerStatus runners={[runner({ roots: ['dimagi'], spans_trees: true })]} />)
    expect(screen.getByTestId('runner-spans-trees-jj-mbp').textContent).toContain('dimagi and another tree')
  })

  it('says nothing for a runner in one tree', () => {
    render(<RunnerStatus runners={[runner({ roots: ['dimagi'], spans_trees: false })]} />)
    expect(screen.queryByTestId('runner-spans-trees-jj-mbp')).toBeNull()
  })
})
