// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { RunnerAlerts } from './RunnerAlerts'
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

describe('RunnerAlerts', () => {
  it('renders nothing when every runner is on main and current', () => {
    const { container } = render(
      <RunnerAlerts
        runners={[runner('ok', { code_branch: 'main', code_sha: SHIPPED, expected_code_sha: SHIPPED })]}
        retiringId={null}
        onRetire={() => {}}
      />,
    )
    expect(container.innerHTML).toBe('')
  })

  it('a heartbeating runner on a branch gets the fix-on-machine variant, with no retire button', () => {
    render(
      <RunnerAlerts
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
      <RunnerAlerts
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
      <RunnerAlerts
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
      <RunnerAlerts
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
      <RunnerAlerts
        runners={[runner('dead', { code_sha: OLD, expected_code_sha: SHIPPED, status: 'stale' })]}
        retiringId={null}
        onRetire={() => {}}
      />,
    )
    expect(screen.getByTestId('retire-runner-dead')).toBeTruthy()
  })
})

describe('RunnerAlerts — a runner AHEAD of the deploy', () => {
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
    render(<RunnerAlerts runners={[ahead]} retiringId={null} onRetire={() => {}} />)
    expect(screen.queryByText(/install-runner\.sh/)).toBeNull()
    expect(screen.queryByText(/out of date/i)).toBeNull()
  })

  it('says which direction it differs in, rather than implying a fault', () => {
    render(<RunnerAlerts runners={[ahead]} retiringId={null} onRetire={() => {}} />)
    const box = screen.getByTestId('runner-code-alert-mbp')
    expect(box.getAttribute('data-alert-kind')).toBe('ahead')
    expect(box.textContent).toMatch(/ahead of/i)
  })

  it('is not styled as an error — nothing is broken', () => {
    render(<RunnerAlerts runners={[ahead]} retiringId={null} onRetire={() => {}} />)
    const box = screen.getByTestId('runner-code-alert-mbp')
    expect(box.className).not.toContain('border-destructive')
  })

  it('offers no retire button even when the box is quiet', () => {
    // Retire is the escape hatch for an alert that can never clear on its own.
    // This one clears itself on the next deploy, so offering to permanently
    // destroy the runner row would be wildly disproportionate.
    render(
      <RunnerAlerts
        runners={[{ ...ahead, status: 'stale' } as RunnerOut]}
        retiringId={null}
        onRetire={() => {}}
      />,
    )
    expect(screen.queryByTestId('retire-runner-mbp')).toBeNull()
  })
})

describe('RunnerAlerts — a runner that has gone dark', () => {
  // The banner IS the fix here. The old product said "⚠ Offline runner is out of
  // date" for a box that had been unattended for three days, which invites
  // exactly one question — why is auto-update not handling this? — and answers
  // none of it. Assert the copy, because the copy is the deliverable.
  const dark = (fields: Partial<RunnerOut> = {}) =>
    runner('acedimagi-mbp-cdp', {
      kind: 'emdash',
      status: 'stale',
      host: 'acedimagi@Jonathans-MacBook-Pro.local',
      last_heartbeat_at: new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString(),
      ...fields,
    })

  it('leads with the silence and says the updater is down too', () => {
    render(
      <RunnerAlerts runners={[dark({ code_sha: OLD, expected_code_sha: SHIPPED, code_version: '0.1.0' })]} retiringId={null} onRetire={() => {}} />,
    )
    const alert = screen.getByTestId('runner-code-alert-acedimagi-mbp-cdp')
    expect(alert.dataset.alertKind).toBe('dark')
    expect(alert.textContent).toContain('gone dark')
    expect(alert.textContent).toContain('3d')
    expect(alert.textContent).toContain('including its auto-updater')
  })

  it('demotes the sha gap to history, and never tells you to run the installer', () => {
    // Printing `install-runner.sh` under a box nobody can reach is an
    // instruction that cannot be followed — the machine is not there.
    render(
      <RunnerAlerts runners={[dark({ code_sha: OLD, expected_code_sha: SHIPPED })]} retiringId={null} onRetire={() => {}} />,
    )
    const alert = screen.getByTestId('runner-code-alert-acedimagi-mbp-cdp')
    expect(alert.textContent).toContain('history, not a task')
    expect(alert.textContent).not.toContain('install-runner.sh')
  })

  it('names the macOS account to log back in', () => {
    render(<RunnerAlerts runners={[dark()]} retiringId={null} onRetire={() => {}} />)
    expect(screen.getByTestId('runner-code-alert-acedimagi-mbp-cdp').textContent).toContain('acedimagi')
  })

  it('warns that a paused box will still not work once it is back', () => {
    // acedimagi had been paused since Aug 14 and dark since Aug 24. Logging in
    // fixes the second and leaves the first — worth knowing BEFORE walking over.
    render(
      <RunnerAlerts
        runners={[dark({ paused: true, paused_note: 'paused locally (~/.canopy/PAUSED)' })]}
        retiringId={null}
        onRetire={() => {}}
      />,
    )
    const alert = screen.getByTestId('runner-code-alert-acedimagi-mbp-cdp')
    expect(alert.textContent).toContain('also paused')
    expect(alert.textContent).toContain('~/.canopy/PAUSED')
  })

  it('offers Retire, worded for a box that may simply be gone', () => {
    const onRetire = vi.fn()
    render(<RunnerAlerts runners={[dark()]} retiringId={null} onRetire={onRetire} />)
    fireEvent.click(screen.getByTestId('retire-runner-acedimagi-mbp-cdp'))
    expect(onRetire).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('runner-code-alert-acedimagi-mbp-cdp').textContent).toContain('gone for good')
  })

  it('a dark CLOUD runner is not told to log into a macOS account', () => {
    render(
      <RunnerAlerts
        runners={[dark({ kind: 'cloud', host: 'cloud-ec2-1' })]}
        retiringId={null}
        onRetire={() => {}}
      />,
    )
    const alert = screen.getByTestId('runner-code-alert-acedimagi-mbp-cdp')
    expect(alert.textContent).toContain('Bring that box back up')
    expect(alert.textContent).not.toContain('launchd loads')
  })
})
