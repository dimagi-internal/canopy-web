import { parkedReason, type SessionRunnerLiveness } from '@/components/chat/runnerEligibility'
import type { DotTone } from './StateDot'

// The one place a session's status and runner are put into words. Every list
// of sessions (the session panel, /ide) reads this, so a session parked behind
// an offline runner never reads as "running" on one page and "runner offline"
// on another.

/** The fields `sessionDisplay` reads. Structural, so a test can pass a literal
 *  and any session payload (SessionOut, an /ide row) fits. */
export type SessionDisplayInput = SessionRunnerLiveness & {
  running?: boolean | null
  waiting_on_you?: boolean | null
  /** The runner the session is bound to. */
  runner_name?: string | null
  /** The runner its latest turn ran on. */
  turn_runner_name?: string | null
  /** The runner it was asked to start on, before its first message binds it. */
  requested_runner_name?: string | null
}

export type SessionDisplay = {
  tone: DotTone
  /** "runner offline", "runner paused", "waiting on you", "running" or "idle". */
  label: string
  /** The runner to name for it; a requested-but-unbound runner reads "→ name".
   *  Null when it has no runner at all (an unbound web chat). */
  runner: string | null
  /** `runner` names the runner it will start on, not one it is on. */
  runnerRequested: boolean
}

/**
 * Status, in one precedence:
 * 1. held — its bound runner is offline or paused, so nothing it shows can move
 *    until that box is back (a pending question or a stale `running` included);
 * 2. asking — an agent is blocked on a question to a person; it outranks
 *    `running` because the engine is parked on it;
 * 3. running;
 * 4. idle.
 *
 * Runner: the bound runner, else the one its latest turn ran on, else the one
 * it was asked to start on.
 */
export function sessionDisplay(s: SessionDisplayInput): SessionDisplay {
  const parked = parkedReason(s)
  const status: Pick<SessionDisplay, 'tone' | 'label'> = parked
    ? { tone: 'held', label: `runner ${parked}` }
    : s.waiting_on_you
      ? { tone: 'asking', label: 'waiting on you' }
      : s.running
        ? { tone: 'running', label: 'running' }
        : { tone: 'idle', label: 'idle' }
  const bound = s.runner_name || s.turn_runner_name || null
  const requested = !bound && s.requested_runner_name ? s.requested_runner_name : null
  return {
    ...status,
    runner: bound ?? (requested ? `→ ${requested}` : null),
    runnerRequested: requested !== null,
  }
}
