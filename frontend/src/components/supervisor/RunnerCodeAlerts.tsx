import type { JSX } from 'react'
import type { RunnerOut } from '@/api/harness'
import { runnerCodeAlerts, type RunnerCodeAlert } from './codeProvenance'

// LOUD alert: this runner is not executing the code we think it is. Two faults
// (see codeProvenance.ts) — a source checkout on the wrong branch, or an
// install that is behind what shipped. In both cases a HEARTBEATING runner is
// fixed on its machine, while a QUIET one can never clear on its own (what is
// shown is its last report), so the in-place resolve there is Retire.
const UPDATE_CMD = 'runner/canopy_runner/scripts/install-runner.sh'
const BRANCH_CMD = 'git -C ~/emdash-projects/canopy-web checkout main && git pull'

function headline({ kind, unreachable }: RunnerCodeAlert): string {
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

export function RunnerCodeAlerts({
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
      {runnerCodeAlerts(runners).map((alert) => {
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

            {kind === 'ahead' ? (
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
                from{' '}
                <span className="rounded bg-destructive/20 px-1 font-mono font-semibold">
                  {r.code_sha.slice(0, 12)}
                </span>
                , but{' '}
                <span className="rounded bg-destructive/20 px-1 font-mono font-semibold">
                  {r.expected_code_sha.slice(0, 12)}
                </span>{' '}
                has shipped.{' '}
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

            {kind !== 'ahead' && (
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
                  Retiring is permanent for this row and clears the alert; re-pairing later mints a
                  fresh runner.
                </p>
              </>
            )}
          </div>
        )
      })}
    </>
  )
}
