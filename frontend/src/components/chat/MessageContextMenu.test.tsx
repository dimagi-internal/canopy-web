// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import type { RunnerOut } from '@/api/harness'
import { MessageContextMenu, forkBlockedReason, turnIndexAt } from './MessageContextMenu'

const runner = (name: string, over: Partial<RunnerOut> = {}) =>
  ({ id: name, name, kind: 'emdash', engine: 'claude-cli', status: 'online', ...over }) as RunnerOut

afterEach(cleanup)

describe('forkBlockedReason', () => {
  it('allows a cloud runner, or a laptop on the Claude CLI runtime', () => {
    expect(forkBlockedReason('mbp', [runner('mbp')])).toBeNull()
    expect(forkBlockedReason('ec2', [runner('ec2', { kind: 'cloud', engine: 'emdash' })])).toBeNull()
  })

  it('says why anything else cannot fork', () => {
    expect(forkBlockedReason('', [runner('mbp')])).toMatch(/not run on a runner/)
    expect(forkBlockedReason('gone', [runner('mbp')])).toMatch(/not one of your runners/)
    expect(forkBlockedReason('mbp', [runner('mbp', { engine: 'emdash' })])).toMatch(/Claude CLI runtime; mbp runs emdash/)
  })
})

describe('turnIndexAt', () => {
  it('reads the nearest row the click landed in', () => {
    document.body.innerHTML =
      '<details data-turn-index="128"><div data-turn-index="192"><span id="t">x</span></div></details><p id="o">o</p>'
    expect(turnIndexAt(document.getElementById('t'))).toBe(192)
    expect(turnIndexAt(document.getElementById('o'))).toBeNull()
    expect(turnIndexAt(null)).toBeNull()
  })
})

describe('MessageContextMenu', () => {
  it('forks on click', () => {
    const onFork = vi.fn()
    render(<MessageContextMenu x={5} y={5} blocked={null} busy={false} error={null} onFork={onFork} onDismiss={() => {}} />)
    fireEvent.click(screen.getByTestId('message-menu-fork'))
    expect(onFork).toHaveBeenCalledOnce()
  })

  it('shows why it is blocked, and the server refusal', () => {
    render(
      <MessageContextMenu x={5} y={5} blocked="Forking needs a cloud runner or the Claude CLI runtime; mbp runs emdash" busy={false}
        error="the last turn is still running" onFork={() => {}} onDismiss={() => {}} />,
    )
    expect(screen.getByTestId('message-menu-fork')).toHaveProperty('disabled', true)
    expect(screen.getByText(/mbp runs emdash/)).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toMatch(/still running/)
  })
})
