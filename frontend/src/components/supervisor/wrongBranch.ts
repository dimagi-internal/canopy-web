// The wrong-branch banner's logic, extracted from SupervisorPage so it's
// unit-testable. A runner heartbeats the git branch of the checkout it imports
// code from; anything but `main` means its turns execute stale/unreviewed code.
import type { RunnerOut } from '@/api/harness'

export type BranchAlert = {
  runner: RunnerOut
  // A quiet runner (stale/disconnected) can never heartbeat its way off the
  // wrong branch — the branch shown is just its LAST report, so without action
  // the banner sits forever (the 2026-07-25 jj-mbp-cdp incident: its macOS
  // account logged out mid-branch). For those, the in-place resolve is to
  // retire the runner; a heartbeating one is fixed on its machine instead.
  unreachable: boolean
}

export function wrongBranchAlerts(runners: readonly RunnerOut[] | null): BranchAlert[] {
  return (runners ?? [])
    .filter((r) => !!r.code_branch && r.code_branch !== 'main')
    .map((runner) => ({
      runner,
      unreachable: runner.status === 'stale' || runner.status === 'disconnected',
    }))
}
