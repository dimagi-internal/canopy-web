import { describe, expect, it } from 'vitest'

import { runnerCodeAlerts } from './codeProvenance'
import type { RunnerOut } from '@/api/harness'

// RunnerOut has many fields; the helper only reads these.
const runner = (
  name: string,
  fields: Partial<RunnerOut> & { status?: string } = {},
): RunnerOut =>
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

describe('runnerCodeAlerts — branch (source-mode runners)', () => {
  it('alerts only runners on a non-main, non-empty branch', () => {
    const rows = [
      runner('good', { code_branch: 'main' }),
      runner('installed', { code_branch: '' }), // no checkout — never a branch alert
      runner('bad', { code_branch: 'ddd-ui-polish' }),
    ]
    expect(runnerCodeAlerts(rows).map((a) => a.runner.name)).toEqual(['bad'])
    expect(runnerCodeAlerts(rows)[0].kind).toBe('branch')
  })

  it('a heartbeating runner is reachable — fix-on-machine, no retire offer', () => {
    for (const status of ['online', 'degraded']) {
      const [alert] = runnerCodeAlerts([runner('r', { code_branch: 'feat-x', status })])
      expect(alert.unreachable).toBe(false)
    }
  })

  it('a quiet runner is unreachable — its branch is a LAST report and only retiring clears it', () => {
    // The 2026-07-25 incident: jj-mbp-cdp died on ddd-ui-polish after its
    // account logged out; the banner could never clear on its own.
    for (const status of ['stale', 'disconnected']) {
      const [alert] = runnerCodeAlerts([runner('r', { code_branch: 'ddd-ui-polish', status })])
      expect(alert.unreachable).toBe(true)
    }
  })
})

describe('runnerCodeAlerts — outdated (installed runners)', () => {
  it('alerts when the installed sha differs from what shipped', () => {
    const [alert] = runnerCodeAlerts([
      runner('mbp', { code_sha: OLD, expected_code_sha: SHIPPED, code_version: '0.1.0' }),
    ])
    expect(alert.kind).toBe('outdated')
    expect(alert.runner.name).toBe('mbp')
  })

  it('stays silent when the runner is current', () => {
    expect(
      runnerCodeAlerts([runner('mbp', { code_sha: SHIPPED, expected_code_sha: SHIPPED })]),
    ).toEqual([])
  })

  it('stays silent when EITHER side is unknown', () => {
    // Empty means unknown, never "different". The cloud runner is a separate
    // program that reports no sha; a dev server bakes in no expectation; a
    // shallow clone yields neither. Alerting on partial information would cry
    // wolf on exactly the boxes we know least about.
    const rows = [
      runner('cloud', { code_sha: '', expected_code_sha: SHIPPED }),
      runner('dev-server', { code_sha: OLD, expected_code_sha: '' }),
      runner('both-unknown', { code_sha: '', expected_code_sha: '' }),
    ]
    expect(runnerCodeAlerts(rows)).toEqual([])
  })

  it('a quiet outdated runner offers the retire escape hatch too', () => {
    const [alert] = runnerCodeAlerts([
      runner('dead', { code_sha: OLD, expected_code_sha: SHIPPED, status: 'stale' }),
    ])
    expect(alert.unreachable).toBe(true)
  })
})

describe('runnerCodeAlerts — direction (a sha is a name, not a position)', () => {
  // `code_sha !== expected_code_sha` means DIFFERENT. It was being reported as
  // "behind", which is an inference the data does not support — there are three
  // ways to differ and only one is "update this box". Observed 2026-07-29:
  // jj-mbp-cdp, freshly installed from origin/main, was flagged outdated while
  // being the most current box in the fleet — the deploy simply hadn't caught up.
  // The commit TIMESTAMP is what orders them, and both sides already run
  // `git log -1`, so it costs one extra format specifier.
  const T_OLD = 1753000000
  const T_NEW = 1753999999

  it('says BEHIND when the runner commit predates what shipped', () => {
    const [alert] = runnerCodeAlerts([
      runner('mbp', {
        code_sha: OLD, expected_code_sha: SHIPPED,
        code_committed_at: T_OLD, expected_code_committed_at: T_NEW,
      }),
    ])
    expect(alert.kind).toBe('outdated')
  })

  it('says AHEAD — not behind — when the runner commit is newer than what shipped', () => {
    // The false alarm. Telling someone to update the newest box in the fleet
    // teaches them the banner is noise, which is how a real one gets ignored.
    const [alert] = runnerCodeAlerts([
      runner('mbp', {
        code_sha: SHIPPED, expected_code_sha: OLD,
        code_committed_at: T_NEW, expected_code_committed_at: T_OLD,
      }),
    ])
    expect(alert.kind).toBe('ahead')
  })

  it('falls back to "different" when the timestamps cannot order them', () => {
    // A runner too old to report a timestamp is exactly the one most likely to
    // be genuinely behind — so an unknown ordering must NOT silence the alert.
    // It keeps today's behaviour and only loses the direction.
    const [alert] = runnerCodeAlerts([
      runner('legacy', { code_sha: OLD, expected_code_sha: SHIPPED }),
    ])
    expect(alert.kind).toBe('outdated')
  })

  it('stays silent when the shas match, whatever the timestamps say', () => {
    // Same code is same code. A timestamp disagreement here would mean one side
    // computed it wrong, and inventing a banner from that helps nobody.
    expect(
      runnerCodeAlerts([
        runner('mbp', {
          code_sha: SHIPPED, expected_code_sha: SHIPPED,
          code_committed_at: T_OLD, expected_code_committed_at: T_NEW,
        }),
      ]),
    ).toEqual([])
  })

  it('an AHEAD runner still reports reachability, so the UI can offer retire', () => {
    const [alert] = runnerCodeAlerts([
      runner('dead', {
        code_sha: SHIPPED, expected_code_sha: OLD,
        code_committed_at: T_NEW, expected_code_committed_at: T_OLD,
        status: 'stale',
      }),
    ])
    expect(alert.unreachable).toBe(true)
  })
})

describe('runnerCodeAlerts — shared', () => {
  it('raises ONE banner per runner: a wrong branch outranks being out of date', () => {
    // A source runner on a branch will almost always also look "outdated"
    // (its sha is whatever that branch last touched). Two banners about the
    // same box would just be noise, and the branch is the louder fault.
    const alerts = runnerCodeAlerts([
      runner('r', { code_branch: 'feat-x', code_sha: OLD, expected_code_sha: SHIPPED }),
    ])
    expect(alerts).toHaveLength(1)
    expect(alerts[0].kind).toBe('branch')
  })

  it('a source runner on main that is current raises nothing', () => {
    expect(
      runnerCodeAlerts([
        runner('r', { code_branch: 'main', code_sha: SHIPPED, expected_code_sha: SHIPPED }),
      ]),
    ).toEqual([])
  })

  it('tolerates a null runner list (initial load)', () => {
    expect(runnerCodeAlerts(null)).toEqual([])
  })
})
