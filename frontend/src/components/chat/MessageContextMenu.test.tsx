// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import type { RunnerOut } from '@/api/harness'
import { MessageContextMenu, canFork, turnIndexAt } from './MessageContextMenu'

const runner = (name: string, over: Partial<RunnerOut> = {}) =>
  ({ id: name, name, kind: 'emdash', engine: 'claude-cli', status: 'online', ...over }) as RunnerOut

afterEach(cleanup)

describe('canFork', () => {
  it('allows a cloud runner, or a laptop on the Claude CLI runtime', () => {
    expect(canFork('mbp', [runner('mbp')])).toBe(true)
    expect(canFork('ec2', [runner('ec2', { kind: 'cloud', engine: 'emdash' })])).toBe(true)
  })

  it('refuses anything else', () => {
    expect(canFork('', [runner('mbp')])).toBe(false)
    expect(canFork('gone', [runner('mbp')])).toBe(false)
    expect(canFork('mbp', [runner('mbp', { engine: 'emdash' })])).toBe(false)
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
    render(<MessageContextMenu x={5} y={5} busy={false} error={null} onFork={onFork} onDismiss={() => {}} />)
    fireEvent.click(screen.getByTestId('message-menu-fork'))
    expect(onFork).toHaveBeenCalledOnce()
  })

  it('shows the server refusal', () => {
    render(
      <MessageContextMenu x={5} y={5} busy={false} error="the last turn is still running" onFork={() => {}} onDismiss={() => {}} />,
    )
    expect(screen.getByRole('alert').textContent).toMatch(/still running/)
  })

  it('copies the row and closes, with no Fork where the runner cannot cut one', () => {
    const writeText = vi.fn(() => Promise.resolve())
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    const onDismiss = vi.fn()
    render(
      <MessageContextMenu x={5} y={5} busy={false} error={null} copyText="the words" canFork={false}
        onFork={() => {}} onDismiss={onDismiss} />,
    )
    expect(screen.queryByTestId('message-menu-fork')).toBeNull()
    fireEvent.click(screen.getByTestId('message-menu-copy'))
    expect(writeText).toHaveBeenCalledWith('the words')
    expect(onDismiss).toHaveBeenCalled()
  })
})
