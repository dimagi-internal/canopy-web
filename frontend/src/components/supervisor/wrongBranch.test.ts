import { describe, expect, it } from 'vitest'

import { wrongBranchAlerts } from './wrongBranch'
import type { RunnerOut } from '@/api/harness'

// RunnerOut has many fields; the helper only reads code_branch/status.
const runner = (name: string, code_branch: string, status: string): RunnerOut =>
  ({ id: name, name, code_branch, status } as unknown as RunnerOut)

describe('wrongBranchAlerts', () => {
  it('alerts only runners on a non-main, non-empty branch', () => {
    const rows = [
      runner('good', 'main', 'online'),
      runner('cloud', '', 'online'), // no checkout — never alerts
      runner('bad', 'ddd-ui-polish', 'online'),
    ]
    expect(wrongBranchAlerts(rows).map((a) => a.runner.name)).toEqual(['bad'])
  })

  it('a heartbeating runner is reachable — fix-on-machine, no retire offer', () => {
    for (const status of ['online', 'degraded']) {
      const [alert] = wrongBranchAlerts([runner('r', 'feat-x', status)])
      expect(alert.unreachable).toBe(false)
    }
  })

  it('a quiet runner is unreachable — its branch is a LAST report and only retiring clears it', () => {
    // The 2026-07-25 incident: jj-mbp-cdp died on ddd-ui-polish after its
    // account logged out; the banner could never clear on its own.
    for (const status of ['stale', 'disconnected']) {
      const [alert] = wrongBranchAlerts([runner('r', 'ddd-ui-polish', status)])
      expect(alert.unreachable).toBe(true)
    }
  })

  it('tolerates a null runner list (initial load)', () => {
    expect(wrongBranchAlerts(null)).toEqual([])
  })
})
