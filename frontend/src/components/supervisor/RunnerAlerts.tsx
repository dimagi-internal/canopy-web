import type { JSX } from 'react'
import type { RunnerOut } from '@/api/harness'
import { humanizeSilence, macAccount, runnerAlerts, type RunnerAlert } from './runnerAlertRules'

// LOUD alert: something is wrong with this runner. Three faults, ranked in
// runnerAlertRules.ts — it has gone dark, its source checkout is on the wrong
// branch, or its install is behind what shipped. A HEARTBEATING runner is fixed
// on its machine; a QUIET one can never clear on its own (what is shown is its
// last report), so the in-place resolve there is Retire.
const UPDATE_CMD = 'runner/canopy_runner/scripts/install-runner.sh'
const BRANCH_CMD = 'git -C ~/emdash-projects/canopy-web checkout main && git pull'

function headline({ kind, unreachable, silentForMs }: RunnerAlert): string {
  if (kind === 'dark') return `⚠ Runner has gone dark — ${humanizeSilence(silentForMs ?? 0)} with no heartbeat`
  if (kind === 'branch') {
    return unreachable
      ? '⚠ Offline runner stuck on wrong branch'
      : '⚠ Runner on wrong branch — stale code'
  }
  // Nothing is broken and there is nothing to run: this box is NEWER than what
  // shipped, so the deploy is what catches up. Deliberately not "⚠", not
  // destructive, and not actionable.
  if (kind === 'ahead') return 'Runner is ahead of the deploy'
  return unreachable ? '⚠ Offline runner is out of date' : '⚠ Runner is out of date'
}

const Sha = ({ value }: { value: string }): JSX.Element => (
  <span className="rounded bg-destructive/20 px-1 font-mono font-semibold">{value.slice(0, 12)}</span>
)

/**
 * The dark-runner body.
 *
 * It leads with the silence and then explains, in the banner itself, why no
 * automation is going to resolve this. That explanation is the feature: the
 * auto-updater is deliberately a SEPARATE launchd job so that a crash-looping
 * runner can still be rescued (see com.canopy.runner.updater.plist.template),
 * and it is easy — we did it — to read that as "a dark box heals itself". It
 * does not. Both jobs are LaunchAgents in the same macOS login session, so the
 * one failure that takes out the runner by taking out its whole user session
 * takes out its rescuer at the same instant. Whatever the box last reported
 * about its code is shown as history, never as an instruction.
 */
function DarkBody({ runner: r, silentForMs }: RunnerAlert): JSX.Element {
  const account = macAccount(r.host)
  const behind =
    r.code_sha && r.expected_code_sha && r.code_sha !== r.expected_code_sha
      ? { mine: r.code_sha, shipped: r.expected_code_sha }
      : null
  return (
    <>
      <p className="mt-1 text-[13px] leading-snug">
        <span className="font-semibold">{r.name}</span>
        {r.host ? <> ({r.host})</> : null} last checked in{' '}
        <span className="font-semibold">{humanizeSilence(silentForMs ?? 0)} ago</span>. Nothing on
        that box is running —{' '}
        <span className="font-semibold">including its auto-updater</span>, which is a launchd agent
        in the same login session as the runner. It cannot update, self-heal, or clear this alert
        on its own.
      </p>
      {behind && (
        <p className="mt-1 text-[13px] leading-snug">
          Its last report was runner{' '}
          <span className="font-mono font-semibold">{r.code_version || 'unknown'}</span> built from{' '}
          <Sha value={behind.mine} /> and <Sha value={behind.shipped} /> has since shipped — but
          that is history, not a task: the gap cannot close until the box is back.
        </p>
      )}
      {r.paused && (
        <p className="mt-1 text-[13px] leading-snug">
          It is <span className="font-semibold">also paused</span>
          {r.paused_note ? <> ({r.paused_note})</> : null}
          {r.paused_at ? <> since {new Date(r.paused_at).toLocaleDateString()}</> : null}, so
          bringing it back online will not resume work until it is unpaused.
        </p>
      )}
      <p className="mt-1.5 break-words text-[12px] leading-snug opacity-90">
        {r.kind === 'emdash' ? (
          <>
            Log back into{' '}
            {account ? (
              <span className="font-mono font-semibold">{account}</span>
            ) : (
              'that macOS account'
            )}{' '}
            on that machine — launchd loads the runner and its updater at login, and the updater
            then installs what shipped within 30 minutes.
          </>
        ) : (
          <>Bring that box back up; it resumes heartbeating and updating on its own once it does.</>
        )}
      </p>
    </>
  )
}

export function RunnerAlerts({
  runners,
  retiringId,
  onRetire,
}: {
  runners: readonly RunnerOut[] | null
  retiringId: string | null
  onRetire: (runner: RunnerOut) => void
}): JSX.Element {
  return (
    <>
      {runnerAlerts(runners).map((alert) => {
        const { runner: r, kind, unreachable } = alert
        return (
          <div
            key={`code-alert-${r.id}`}
            role="alert"
            data-testid={`runner-code-alert-${r.id}`}
            data-alert-kind={kind}
            className={
              kind === 'ahead'
                ? 'rounded-lg border border-border bg-muted p-3 text-muted-foreground'
                : 'rounded-lg border-2 border-destructive bg-destructive/15 p-3 text-destructive'
            }
          >
            <p className="text-[13px] font-bold uppercase tracking-wide">{headline(alert)}</p>

            {kind === 'dark' ? (
              <DarkBody {...alert} />
            ) : kind === 'ahead' ? (
              <p className="mt-1 text-[13px] leading-snug">
                <span className="font-semibold">{r.name}</span> is running runner code from{' '}
                <span className="rounded bg-foreground/10 px-1 font-mono font-semibold">
                  {r.code_sha.slice(0, 12)}
                </span>
                , which is <span className="font-semibold">newer</span> than the{' '}
                <span className="rounded bg-foreground/10 px-1 font-mono font-semibold">
                  {r.expected_code_sha.slice(0, 12)}
                </span>{' '}
                the server expects. Nothing to do — the next deploy catches up. (If it stays
                this way, someone installed a branch here on purpose.)
              </p>
            ) : kind === 'branch' ? (
              <p className="mt-1 text-[13px] leading-snug">
                <span className="font-semibold">{r.name}</span>{' '}
                {unreachable ? 'last reported branch' : 'is running on branch'}{' '}
                <span className="rounded bg-destructive/20 px-1 font-mono font-semibold">
                  {r.code_branch}
                </span>
                , not <span className="font-mono">main</span>.{' '}
                {unreachable ? (
                  <>
                    It has <span className="font-semibold">stopped heartbeating</span>, so this
                    alert can never clear on its own.
                  </>
                ) : (
                  <>
                    Its turns are executing <span className="font-semibold">stale / wrong code</span>{' '}
                    — another process likely checked out a branch in the runner's checkout.
                  </>
                )}
              </p>
            ) : (
              <p className="mt-1 text-[13px] leading-snug">
                <span className="font-semibold">{r.name}</span> is running runner{' '}
                <span className="font-mono font-semibold">{r.code_version || 'unknown'}</span> built
                from <Sha value={r.code_sha} />, but <Sha value={r.expected_code_sha} /> has
                shipped.{' '}
                {unreachable ? (
                  <>
                    It has <span className="font-semibold">stopped heartbeating</span>, so this
                    alert can never clear on its own.
                  </>
                ) : (
                  <>
                    Its turns are executing an <span className="font-semibold">older runner</span>.
                  </>
                )}
              </p>
            )}

            {kind !== 'ahead' && kind !== 'dark' && (
              <p className="mt-1.5 break-words text-[12px] leading-snug opacity-90">
                {unreachable ? 'Bring it back on that machine:' : 'Run this on that machine:'}
                <br />
                <span className="font-mono">{kind === 'branch' ? BRANCH_CMD : UPDATE_CMD}</span>
              </p>
            )}

            {/* Retire is the escape hatch for an alert that can never clear on its
                own. An `ahead` runner clears on the next deploy, so offering to
                permanently destroy its row would be wildly disproportionate. */}
            {unreachable && kind !== 'ahead' && (
              <>
                <button
                  type="button"
                  data-testid={`retire-runner-${r.id}`}
                  disabled={retiringId === r.id}
                  onClick={() => onRetire(r)}
                  className="mt-2 rounded-md border border-destructive bg-destructive px-2.5 py-1 text-[12px] font-semibold text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
                >
                  {retiringId === r.id ? 'Retiring…' : 'Retire runner'}
                </button>
                <p className="mt-1 text-[11px] leading-snug opacity-80">
                  {kind === 'dark'
                    ? 'Only if this box is gone for good — retiring is permanent for this row, and re-pairing later mints a fresh one.'
                    : 'Retiring is permanent for this row and clears the alert; re-pairing later mints a fresh runner.'}
                </p>
              </>
            )}
          </div>
        )
      })}
    </>
  )
}
