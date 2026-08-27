import { describe, expect, it } from 'vitest'

import { DARK_AFTER_MS, humanizeSilence, macAccount, runnerAlerts } from './runnerAlertRules'
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

describe('runnerAlerts — branch (source-mode runners)', () => {
  it('alerts only runners on a non-main, non-empty branch', () => {
    const rows = [
      runner('good', { code_branch: 'main' }),
      runner('installed', { code_branch: '' }), // no checkout — never a branch alert
      runner('bad', { code_branch: 'ddd-ui-polish' }),
    ]
    expect(runnerAlerts(rows).map((a) => a.runner.name)).toEqual(['bad'])
    expect(runnerAlerts(rows)[0].kind).toBe('branch')
  })

  it('a heartbeating runner is reachable — fix-on-machine, no retire offer', () => {
    for (const status of ['online', 'degraded']) {
      const [alert] = runnerAlerts([runner('r', { code_branch: 'feat-x', status })])
      expect(alert.unreachable).toBe(false)
    }
  })

  it('a quiet runner is unreachable — its branch is a LAST report and only retiring clears it', () => {
    // The 2026-07-25 incident: jj-mbp-cdp died on ddd-ui-polish after its
    // account logged out; the banner could never clear on its own.
    for (const status of ['stale', 'disconnected']) {
      const [alert] = runnerAlerts([runner('r', { code_branch: 'ddd-ui-polish', status })])
      expect(alert.unreachable).toBe(true)
    }
  })
})

describe('runnerAlerts — outdated (installed runners)', () => {
  it('alerts when the installed sha differs from what shipped', () => {
    const [alert] = runnerAlerts([
      runner('mbp', { code_sha: OLD, expected_code_sha: SHIPPED, code_version: '0.1.0' }),
    ])
    expect(alert.kind).toBe('outdated')
    expect(alert.runner.name).toBe('mbp')
  })

  it('stays silent when the runner is current', () => {
    expect(
      runnerAlerts([runner('mbp', { code_sha: SHIPPED, expected_code_sha: SHIPPED })]),
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
    expect(runnerAlerts(rows)).toEqual([])
  })

  it('a quiet outdated runner offers the retire escape hatch too', () => {
    const [alert] = runnerAlerts([
      runner('dead', { code_sha: OLD, expected_code_sha: SHIPPED, status: 'stale' }),
    ])
    expect(alert.unreachable).toBe(true)
  })
})

describe('runnerAlerts — direction (a sha is a name, not a position)', () => {
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
    const [alert] = runnerAlerts([
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
    const [alert] = runnerAlerts([
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
    const [alert] = runnerAlerts([
      runner('legacy', { code_sha: OLD, expected_code_sha: SHIPPED }),
    ])
    expect(alert.kind).toBe('outdated')
  })

  it('stays silent when the shas match, whatever the timestamps say', () => {
    // Same code is same code. A timestamp disagreement here would mean one side
    // computed it wrong, and inventing a banner from that helps nobody.
    expect(
      runnerAlerts([
        runner('mbp', {
          code_sha: SHIPPED, expected_code_sha: SHIPPED,
          code_committed_at: T_OLD, expected_code_committed_at: T_NEW,
        }),
      ]),
    ).toEqual([])
  })

  it('an AHEAD runner still reports reachability, so the UI can offer retire', () => {
    const [alert] = runnerAlerts([
      runner('dead', {
        code_sha: SHIPPED, expected_code_sha: OLD,
        code_committed_at: T_NEW, expected_code_committed_at: T_OLD,
        status: 'stale',
      }),
    ])
    expect(alert.unreachable).toBe(true)
  })
})

describe('runnerAlerts — shared', () => {
  it('raises ONE banner per runner: a wrong branch outranks being out of date', () => {
    // A source runner on a branch will almost always also look "outdated"
    // (its sha is whatever that branch last touched). Two banners about the
    // same box would just be noise, and the branch is the louder fault.
    const alerts = runnerAlerts([
      runner('r', { code_branch: 'feat-x', code_sha: OLD, expected_code_sha: SHIPPED }),
    ])
    expect(alerts).toHaveLength(1)
    expect(alerts[0].kind).toBe('branch')
  })

  it('a source runner on main that is current raises nothing', () => {
    expect(
      runnerAlerts([
        runner('r', { code_branch: 'main', code_sha: SHIPPED, expected_code_sha: SHIPPED }),
      ]),
    ).toEqual([])
  })

  it('tolerates a null runner list (initial load)', () => {
    expect(runnerAlerts(null)).toEqual([])
  })
})

describe('runnerAlerts — dark (the box itself is gone)', () => {
  // 2026-08-27, acedimagi-mbp-cdp: silent for three days because its macOS
  // account was logged out, which takes down the runner AND the separate
  // launchd updater that exists to rescue it. The only banner in the product
  // said "out of date", so the visible problem was a version number and the
  // actual problem — an unattended box running nothing at all — had no signal.
  const NOW = 1787000000000
  const agoMs = (ms: number) => new Date(NOW - ms).toISOString()
  const HOUR = 60 * 60 * 1000

  it('silence past a day outranks everything the box last reported', () => {
    const [alert] = runnerAlerts(
      [
        runner('acedimagi-mbp-cdp', {
          status: 'stale',
          last_heartbeat_at: agoMs(3 * 24 * HOUR),
          code_sha: OLD,
          expected_code_sha: SHIPPED,
        }),
      ],
      NOW,
    )
    expect(alert.kind).toBe('dark')
    expect(alert.silentForMs).toBe(3 * 24 * HOUR)
    expect(alert.unreachable).toBe(true)
  })

  it('outranks a wrong branch too — one banner, and it is the box, not the code', () => {
    const alerts = runnerAlerts(
      [runner('r', { status: 'stale', last_heartbeat_at: agoMs(2 * 24 * HOUR), code_branch: 'feat-x' })],
      NOW,
    )
    expect(alerts).toHaveLength(1)
    expect(alerts[0].kind).toBe('dark')
  })

  it('a briefly-quiet runner is NOT dark — a closed lid is not an incident', () => {
    // live_status calls a runner stale after 90 SECONDS. Promoting that to a red
    // banner would fire on every laptop every night, which is how a real alert
    // gets ignored — the failure this file already paid for twice.
    const [alert] = runnerAlerts(
      [runner('r', { status: 'stale', last_heartbeat_at: agoMs(2 * HOUR), code_sha: OLD, expected_code_sha: SHIPPED })],
      NOW,
    )
    expect(alert.kind).toBe('outdated')
    expect(alert.unreachable).toBe(true)
  })

  it('is silent for an online runner however old its last heartbeat parses', () => {
    // Only a QUIET runner can be dark. An online row with a lagging timestamp is
    // a clock/propagation artifact, not a dead box.
    expect(
      runnerAlerts([runner('r', { status: 'online', last_heartbeat_at: agoMs(9 * 24 * HOUR) })], NOW),
    ).toEqual([])
  })

  it('is silent when the heartbeat is unknown — never checked in is not a measurable age', () => {
    // Empty means unknown, the same rule the sha comparison follows. A pairing
    // that never started is hygiene (retire it), not an incident to shout about.
    for (const last_heartbeat_at of [null, undefined, 'not-a-date']) {
      expect(runnerAlerts([runner('never', { status: 'disconnected', last_heartbeat_at })], NOW)).toEqual([])
    }
  })

  it('fires exactly at the threshold, not before', () => {
    const at = (ms: number) =>
      runnerAlerts([runner('r', { status: 'stale', last_heartbeat_at: agoMs(ms), code_branch: 'x' })], NOW)[0]
    expect(at(DARK_AFTER_MS - 1).kind).toBe('branch')
    expect(at(DARK_AFTER_MS).kind).toBe('dark')
  })
})

describe('humanizeSilence', () => {
  it('reports the coarsest useful unit', () => {
    expect(humanizeSilence(45 * 60 * 1000)).toBe('45m')
    expect(humanizeSilence(26 * 60 * 60 * 1000)).toBe('26h')
    expect(humanizeSilence(3 * 24 * 60 * 60 * 1000)).toBe('3d')
  })
})

describe('macAccount', () => {
  it('names the macOS account to log back into', () => {
    // The remedy for a dark emdash runner is "log THIS account back in", and
    // naming it is the difference between an instruction and a hint.
    expect(macAccount('acedimagi@Jonathans-MacBook-Pro.local')).toBe('acedimagi')
  })

  it('is empty when host is not user@machine — the caller must fall back', () => {
    for (const host of ['cloud-ec2-1', '', '@nohost']) expect(macAccount(host)).toBe('')
  })
})
