// "Is this runner executing the code we think it is?" — the supervisor's answer,
// extracted from the page so it is unit-testable.
//
// Two ways to be wrong, one banner stack. Which one a runner can trip depends on
// how it was provisioned:
//   - SOURCE-mode (PYTHONPATH into a working checkout): reports a branch, and any
//     branch but `main` means another process left it on stale/wrong code.
//   - INSTALLED (a uv tool venv — the shape since the 2026-07-28 spec): has no
//     branch to be wrong about, but CAN be an old install. It reports the sha of
//     the runner source it was built from, and the server reports the sha that
//     shipped; different means that box is behind.
import type { RunnerOut } from '@/api/harness'

export type RunnerCodeAlert = {
  runner: RunnerOut
  // 'outdated' = older than what shipped, or differing with no way to order the
  // two. 'ahead' = NEWER than what shipped — real, but not a thing to fix on the
  // box: the deploy catches up, or someone installed a branch deliberately.
  kind: 'branch' | 'outdated' | 'ahead'
  // A quiet runner (stale/disconnected) can never heartbeat its way out of
  // either state — what is shown is its LAST report, so without action the
  // banner sits forever (the 2026-07-25 jj-mbp-cdp incident: its macOS account
  // logged out mid-branch). For those the in-place resolve is to retire the
  // runner; a heartbeating one is fixed on its machine instead.
  unreachable: boolean
}

const isUnreachable = (r: RunnerOut): boolean =>
  r.status === 'stale' || r.status === 'disconnected'

export function runnerCodeAlerts(runners: readonly RunnerOut[] | null): RunnerCodeAlert[] {
  const alerts: RunnerCodeAlert[] = []
  for (const runner of runners ?? []) {
    if (runner.code_branch && runner.code_branch !== 'main') {
      alerts.push({ runner, kind: 'branch', unreachable: isUnreachable(runner) })
      continue // one code-provenance banner per runner; the branch is the louder fault
    }
    // BOTH sides must be known. An empty sha means "unknown", not "different":
    // the cloud runner is a separate program that reports none, a dev server has
    // no expectation baked in, and a shallow clone yields nothing. Alerting on
    // partial information would cry wolf on exactly the boxes we know least about.
    if (runner.code_sha && runner.expected_code_sha && runner.code_sha !== runner.expected_code_sha) {
      // A sha is a NAME, not a position: `!==` means different, and there are
      // three ways to differ (older, newer, divergent). Calling all of them
      // "behind" told the operator to update the most current box in the fleet
      // (2026-07-29, jj-mbp-cdp installed from origin/main between a runner
      // change landing and the deploy that ships it). The commit timestamp is
      // what actually orders them; both sides already run `git log -1`.
      //
      // Unknown ordering falls back to 'outdated' rather than silence: a runner
      // too old to report a timestamp is precisely the one most likely to be
      // genuinely behind, so absence of evidence must not clear the alert.
      const mine = runner.code_committed_at ?? 0
      const shipped = runner.expected_code_committed_at ?? 0
      const kind = mine && shipped && mine > shipped ? 'ahead' : 'outdated'
      alerts.push({ runner, kind, unreachable: isUnreachable(runner) })
    }
  }
  return alerts
}
