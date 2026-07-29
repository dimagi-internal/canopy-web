// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { RunnerCodeAlerts } from './RunnerCodeAlerts'
import type { RunnerOut } from '@/api/harness'

const runner = (name: string, fields: Partial<RunnerOut> & { status?: string } = {}): RunnerOut =>
  ({
    id: name,
    name,
    status: 'online',
    code_branch: '',
    code_version: '',
    code_sha: '',
    expected_code_sha: '',
    ...fields,
  }) as unknown as RunnerOut

const SHIPPED = 'a'.repeat(40)
const OLD = 'b'.repeat(40)

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('RunnerCodeAlerts', () => {
  it('renders nothing when every runner is on main and current', () => {
    const { container } = render(
      <RunnerCodeAlerts
        runners={[runner('ok', { code_branch: 'main', code_sha: SHIPPED, expected_code_sha: SHIPPED })]}
        retiringId={null}
        onRetire={() => {}}
      />,
    )
    expect(container.innerHTML).toBe('')
  })

  it('a heartbeating runner on a branch gets the fix-on-machine variant, with no retire button', () => {
    render(
      <RunnerCodeAlerts
        runners={[runner('r1', { code_branch: 'feat-x' })]}
        retiringId={null}
        onRetire={() => {}}
      />,
    )
    const alert = screen.getByTestId('runner-code-alert-r1')
    expect(alert.dataset.alertKind).toBe('branch')
    expect(alert.textContent).toContain('stale / wrong code')
    expect(alert.textContent).toContain('feat-x')
    expect(screen.queryByTestId('retire-runner-r1')).toBeNull()
  })

  it('a dead runner explains the banner can never self-clear and offers Retire', () => {
    const onRetire = vi.fn()
    render(
      <RunnerCodeAlerts
        runners={[runner('jj-mbp-cdp', { code_branch: 'ddd-ui-polish', status: 'stale' })]}
        retiringId={null}
        onRetire={onRetire}
      />,
    )
    const alert = screen.getByTestId('runner-code-alert-jj-mbp-cdp')
    expect(alert.textContent).toContain('stopped heartbeating')
    expect(alert.textContent).toContain('never clear on its own')
    fireEvent.click(screen.getByTestId('retire-runner-jj-mbp-cdp'))
    expect(onRetire).toHaveBeenCalledTimes(1)
    expect(onRetire.mock.calls[0][0].name).toBe('jj-mbp-cdp')
  })

  it('disables the button while that runner is retiring', () => {
    render(
      <RunnerCodeAlerts
        runners={[runner('r1', { code_branch: 'feat-x', status: 'disconnected' })]}
        retiringId="r1"
        onRetire={() => {}}
      />,
    )
    const btn = screen.getByTestId('retire-runner-r1') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(btn.textContent).toContain('Retiring')
  })

  it('an out-of-date runner names both shas and the ONE command that fixes it', () => {
    render(
      <RunnerCodeAlerts
        runners={[
          runner('mbp', { code_version: '0.1.0', code_sha: OLD, expected_code_sha: SHIPPED }),
        ]}
        retiringId={null}
        onRetire={() => {}}
      />,
    )
    const alert = screen.getByTestId('runner-code-alert-mbp')
    expect(alert.dataset.alertKind).toBe('outdated')
    expect(alert.textContent).toContain('out of date')
    expect(alert.textContent).toContain('0.1.0')
    expect(alert.textContent).toContain(OLD.slice(0, 12))
    expect(alert.textContent).toContain(SHIPPED.slice(0, 12))
    // The remedy is the whole point of the banner — a person reading this on a
    // phone should not have to go and look up how to update a runner.
    expect(alert.textContent).toContain('install-runner.sh')
  })

  it('an out-of-date runner that has gone quiet offers Retire', () => {
    render(
      <RunnerCodeAlerts
        runners={[runner('dead', { code_sha: OLD, expected_code_sha: SHIPPED, status: 'stale' })]}
        retiringId={null}
        onRetire={() => {}}
      />,
    )
    expect(screen.getByTestId('retire-runner-dead')).toBeTruthy()
  })
})

describe('RunnerCodeAlerts — a runner AHEAD of the deploy', () => {
  // The false alarm this exists to kill: a box installed from main between a
  // runner change landing and the deploy that ships it is the MOST current in the
  // fleet, and was being told to update itself in destructive red. An alert that
  // fires on the box you just fixed is one you learn to ignore — which costs you
  // the next real one.
  const ahead = runner('mbp', {
    code_sha: SHIPPED,
    expected_code_sha: OLD,
    code_committed_at: 1753999999,
    expected_code_committed_at: 1753000000,
  })

  it('does not tell you to update it', () => {
    render(<RunnerCodeAlerts runners={[ahead]} retiringId={null} onRetire={() => {}} />)
    expect(screen.queryByText(/install-runner\.sh/)).toBeNull()
    expect(screen.queryByText(/out of date/i)).toBeNull()
  })

  it('says which direction it differs in, rather than implying a fault', () => {
    render(<RunnerCodeAlerts runners={[ahead]} retiringId={null} onRetire={() => {}} />)
    const box = screen.getByTestId('runner-code-alert-mbp')
    expect(box.getAttribute('data-alert-kind')).toBe('ahead')
    expect(box.textContent).toMatch(/ahead of/i)
  })

  it('is not styled as an error — nothing is broken', () => {
    render(<RunnerCodeAlerts runners={[ahead]} retiringId={null} onRetire={() => {}} />)
    const box = screen.getByTestId('runner-code-alert-mbp')
    expect(box.className).not.toContain('border-destructive')
  })

  it('offers no retire button even when the box is quiet', () => {
    // Retire is the escape hatch for an alert that can never clear on its own.
    // This one clears itself on the next deploy, so offering to permanently
    // destroy the runner row would be wildly disproportionate.
    render(
      <RunnerCodeAlerts
        runners={[{ ...ahead, status: 'stale' } as RunnerOut]}
        retiringId={null}
        onRetire={() => {}}
      />,
    )
    expect(screen.queryByTestId('retire-runner-mbp')).toBeNull()
  })
})
