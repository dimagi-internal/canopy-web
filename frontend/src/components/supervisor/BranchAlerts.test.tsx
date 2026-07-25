// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { BranchAlerts } from './BranchAlerts'
import type { RunnerOut } from '@/api/harness'

const runner = (name: string, code_branch: string, status: string): RunnerOut =>
  ({ id: name, name, code_branch, status } as unknown as RunnerOut)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('BranchAlerts', () => {
  it('renders nothing when every runner is on main', () => {
    const { container } = render(
      <BranchAlerts runners={[runner('ok', 'main', 'online')]} retiringId={null} onRetire={() => {}} />,
    )
    expect(container.innerHTML).toBe('')
  })

  it('a heartbeating runner gets the fix-on-machine variant, with no retire button', () => {
    render(
      <BranchAlerts runners={[runner('r1', 'feat-x', 'online')]} retiringId={null} onRetire={() => {}} />,
    )
    const alert = screen.getByTestId('runner-branch-alert-r1')
    expect(alert.textContent).toContain('stale / wrong code')
    expect(alert.textContent).toContain('feat-x')
    expect(screen.queryByTestId('retire-runner-r1')).toBeNull()
  })

  it('a dead runner explains the banner can never self-clear and offers Retire', () => {
    const onRetire = vi.fn()
    render(
      <BranchAlerts
        runners={[runner('jj-mbp-cdp', 'ddd-ui-polish', 'stale')]}
        retiringId={null}
        onRetire={onRetire}
      />,
    )
    const alert = screen.getByTestId('runner-branch-alert-jj-mbp-cdp')
    expect(alert.textContent).toContain('stopped heartbeating')
    expect(alert.textContent).toContain('never clear on its own')
    fireEvent.click(screen.getByTestId('retire-runner-jj-mbp-cdp'))
    expect(onRetire).toHaveBeenCalledTimes(1)
    expect(onRetire.mock.calls[0][0].name).toBe('jj-mbp-cdp')
  })

  it('disables the button while that runner is retiring', () => {
    render(
      <BranchAlerts
        runners={[runner('r1', 'feat-x', 'disconnected')]}
        retiringId="r1"
        onRetire={() => {}}
      />,
    )
    const btn = screen.getByTestId('retire-runner-r1') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(btn.textContent).toContain('Retiring')
  })
})
