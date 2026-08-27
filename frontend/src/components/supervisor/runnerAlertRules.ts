// "Is something wrong with this runner?" — the supervisor's answer, extracted
// from the page so it is unit-testable.
//
// Three ways to be wrong, ONE banner per runner, ranked. The ranking is the
// whole point of this module, so read it before adding a fourth:
//
//   1. DARK    — it stopped heartbeating a long time ago. Outranks everything
//                below because everything below is a LAST REPORT: a fact about
//                what that box said before it went away, restated as if it were
//                the present tense.
//   2. BRANCH  — SOURCE-mode (PYTHONPATH into a working checkout): reports a
//                branch, and any branch but `main` means another process left
//                it on stale/wrong code.
//   3. OUTDATED/AHEAD — INSTALLED (a uv tool venv — the shape since the
//                2026-07-28 spec): no branch to be wrong about, but CAN be an
//                old install. It reports the sha of the runner source it was
//                built from, the server reports the sha that shipped.
//
// Rank 1 exists because ranks 2–3 were being used as a proxy for it, and the
// proxy said the wrong thing. 2026-08-27: `acedimagi-mbp-cdp` had not
// heartbeated in three days — its macOS account was logged out, so neither the
// runner NOR its updater (both LaunchAgents in that account's login session)
// was running. The only signal anywhere in the product was "⚠ Offline runner is
// out of date", which names the symptom the dead box last reported and buries
// the cause in an adjective. An operator reading it reasonably asks why
// auto-update is not doing its job; the answer is that nothing on that machine
// is doing any job.
import type { RunnerOut } from '@/api/harness'

// How long a runner may be quiet before silence becomes the headline.
//
// NOT the liveness window: `Runner.live_status` calls a runner `stale` after 90
// SECONDS, which is correct for "can this box claim a turn right now" and would
// be pure noise here — a closed laptop lid would raise a red banner every night.
// A full day of silence is different in kind: no plausible working rhythm
// explains it, and it is well past the point where the box's own 30-minute
// updater timer would have healed anything it could heal.
export const DARK_AFTER_MS = 24 * 60 * 60 * 1000

export type RunnerAlert = {
  runner: RunnerOut
  // 'dark'     = silent past DARK_AFTER_MS. Nothing on that box is running.
  // 'branch'   = source checkout left on a non-main branch.
  // 'outdated' = older than what shipped, or differing with no way to order the
  //              two. 'ahead' = NEWER than what shipped — real, but not a thing
  //              to fix on the box: the deploy catches up, or someone installed
  //              a branch deliberately.
  kind: 'dark' | 'branch' | 'outdated' | 'ahead'
  // Milliseconds of silence, for 'dark' only — the banner leads with it.
  silentForMs?: number
  // A quiet runner can never heartbeat its way out of a branch/sha state — what
  // is shown is its LAST report, so without action the banner sits there. For
  // those the in-place resolve is to retire the runner; a heartbeating one is
  // fixed on its machine instead. Always true on 'dark' (that IS the fault) and
  // reserved on 2–3 for the box that is briefly quiet but not yet dark.
  unreachable: boolean
}

const isQuiet = (r: RunnerOut): boolean => r.status === 'stale' || r.status === 'disconnected'

// Silence we can MEASURE. A null heartbeat is a runner that has never checked in
// at all — paired and never started, most likely — and it deliberately raises
// nothing here: there is no age to report, "0ms ago" and "forever ago" are
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
    if (silent !== null && silent >= DARK_AFTER_MS) {
      // Everything below describes code this box is NOT currently executing.
      alerts.push({ runner, kind: 'dark', silentForMs: silent, unreachable: true })
      continue
    }
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

/** "3d" / "26h" / "45m" — the age of the silence, at the coarsest useful unit. */
export function humanizeSilence(ms: number): string {
  const minutes = Math.floor(ms / 60_000)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return `${hours}h`
  return `${Math.floor(hours / 24)}d`
}

/**
 * The macOS account a laptop runner lives in, from `host` ("user@Machine.local").
 *
 * Load-bearing for the remedy text, not decoration: the fix for a dark emdash
 * runner is to log that specific account back in, and naming it is the
 * difference between an instruction and a hint. Empty when `host` is not in that
 * shape (a cloud row, an old record) — callers must fall back.
 */
export function macAccount(host: string): string {
  const at = (host || '').indexOf('@')
  return at > 0 ? host.slice(0, at) : ''
}
