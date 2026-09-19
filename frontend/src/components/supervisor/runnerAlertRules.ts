// "Is something wrong with this runner?" — the supervisor's answer, extracted
// from the page so it is unit-testable.
//
// Two ways to be wrong, ONE banner per runner, ranked:
//
//   1. BRANCH  — SOURCE-mode (PYTHONPATH into a working checkout): reports a
//                branch, and any branch but `main` means another process left
//                it on stale/wrong code.
//   2. OUTDATED/AHEAD — INSTALLED (a uv tool venv — the shape since the
//                2026-07-28 spec): no branch to be wrong about, but CAN be an
//                old install. It reports the sha of the runner source it was
//                built from, the server reports the sha that shipped.
//
// A runner that has been silent past DARK_AFTER_MS raises NOTHING. It used to
// raise its own red "Runner has gone dark" banner (added 2026-08-27 after
// `acedimagi-mbp-cdp` sat logged-out for three days with only an "out of date"
// banner to show for it). Since 2026-09-19 most of the fleet's runners are
// expected to be dark most of the time — boxes and macOS accounts come and go —
// so a banner per quiet box was a wall of red about the normal state. And a dark
// box's branch/sha is only its LAST report, so it does not fall through to
// ranks 1–2 either: that would be the same noise under a different headline.
import type { RunnerOut } from '@/api/harness'

// How long a runner may be quiet before it is treated as off, and drops out of
// these alerts entirely.
//
// NOT the liveness window: `Runner.live_status` calls a runner `stale` after 90
// SECONDS, which is correct for "can this box claim a turn right now" and would
// the wrong line here — a box quiet for an hour still gets its outdated/branch
// banner (flagged `unreachable`, with Retire). A full day of silence means the
// box is simply off, which is expected.
export const DARK_AFTER_MS = 24 * 60 * 60 * 1000

export type RunnerAlert = {
  runner: RunnerOut
  // 'branch'   = source checkout left on a non-main branch.
  // 'outdated' = older than what shipped, or differing with no way to order the
  //              two. 'ahead' = NEWER than what shipped — real, but not a thing
  //              to fix on the box: the deploy catches up, or someone installed
  //              a branch deliberately.
  kind: 'branch' | 'outdated' | 'ahead'
  // A quiet runner can never heartbeat its way out of a branch/sha state — what
  // is shown is its LAST report, so without action the banner sits there. For
  // those the in-place resolve is to retire the runner; a heartbeating one is
  // fixed on its machine instead.
  unreachable: boolean
}

const isQuiet = (r: RunnerOut): boolean => r.status === 'stale' || r.status === 'disconnected'

// Silence we can MEASURE. A null heartbeat is a runner that has never checked in
// at all — paired and never started, most likely — and it is deliberately not
// treated as dark: there is no age to measure, "0ms ago" and "forever ago" are
// indistinguishable in the data, and the same empty-means-unknown rule the sha
// comparison follows applies to a timestamp. Retiring an unused pairing is a
// hygiene task, not an incident.
function silentFor(r: RunnerOut, now: number): number | null {
  if (!isQuiet(r) || !r.last_heartbeat_at) return null
  const at = Date.parse(r.last_heartbeat_at)
  if (Number.isNaN(at)) return null
  return Math.max(0, now - at)
}

export function runnerAlerts(
  runners: readonly RunnerOut[] | null,
  now: number = Date.now(),
): RunnerAlert[] {
  const alerts: RunnerAlert[] = []
  for (const runner of runners ?? []) {
    const silent = silentFor(runner, now)
    // Dark is expected, not an incident — and everything below would describe
    // code this box is NOT currently executing.
    if (silent !== null && silent >= DARK_AFTER_MS) continue
    const unreachable = isQuiet(runner)

    if (runner.code_branch && runner.code_branch !== 'main') {
      alerts.push({ runner, kind: 'branch', unreachable })
      continue // one banner per runner; the branch is the louder of 2 and 3
    }
    // BOTH sides must be known. An empty sha means "unknown", not "different":
    // a dev server has no expectation baked in, a shallow clone yields nothing,
    // and an unstamped install cannot say. Alerting on partial information would
    // cry wolf on exactly the boxes we know least about.
    //
    // The cloud runner used to be the headline example here — a separate program
    // reporting nothing. Since spec 2026-07-30 it reports its own sha and the
    // server serves it the expectation for ITS path (runner/ec2), so cloud rows
    // now participate in this alert on the same terms as laptops.
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
      alerts.push({ runner, kind, unreachable })
    }
  }
  return alerts
}
