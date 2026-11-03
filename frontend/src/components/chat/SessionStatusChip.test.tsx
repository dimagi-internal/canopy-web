// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { turnView, type SessionState, type TurnStatus } from 'canopy-ui/chat'

import { SessionStatusChip } from './SessionStatusChip'

afterEach(cleanup)

const status = (over: Partial<TurnStatus> = {}): TurnStatus => ({
  state: 'done', agent_slug: null, runners: [], claimed_by: null, pinned: false, cloud_runner: null,
  cloud_runner_id: null, last_seen_at: null, menu_pending: false, settled: true, stuck: false, ...over,
})
const state = (over: Partial<SessionState> = {}): SessionState => ({
  messages: [], active_draft: null, participants: [], presence_user_ids: [], current_user_id: 1, ...over,
})

describe('SessionStatusChip', () => {
  it('says what the turn view says, naming the runner holding the turn', () => {
    const view = turnView({ state: state({ turn_status: status({ state: 'working', settled: false, claimed_by: 'box-2' }) }), runnerFallback: 'box-1' })
    render(<SessionStatusChip view={view} boundRunner="box-1" />)
    const chip = screen.getByTestId('turn-phase')
    expect(chip.getAttribute('data-phase')).toBe('working')
    expect(chip.textContent).toBe('running · box-2')
  })

  it('a reply whose completion was lost reads idle once the server settled the turn — no stale "running"', () => {
    const view = turnView({
      state: state({
        messages: [{
          id: 'a', turn_index: 2, role: 'assistant', content: {}, plaintext: 'part', status: 'streaming',
          error_detail: null, started_at: null, completed_at: null, created_at: '2026-10-31T10:00:00Z',
        }],
        turn_status: status({ finished_at: '2026-10-31T10:05:00Z' }),
      }),
      runnerFallback: 'box-1',
    })
    render(<SessionStatusChip view={view} boundRunner="box-1" />)
    expect(screen.getByTestId('turn-phase').textContent).toBe('idle · box-1')
  })

  it('names the runner a new chat starts on', () => {
    render(<SessionStatusChip view={turnView({ state: state() })} requestedRunner="laptop" />)
    expect(screen.getByTestId('chat-requested-runner').textContent).toContain('starts on laptop')
  })
})
