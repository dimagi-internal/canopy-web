// Pure helpers for "which runners can execute a chat turn" — shared by the
// new-chat "Run on" picker (ChatSessionsPanel) and the offline-runner
// placement banner (ChatPage). Kept pure so both surfaces stay unit-testable
// without mounting React.
import type { RunnerOut } from "@/api/harness";

/**
 * Only a session-CAPABLE runner (capabilities.sessions === true) can execute
 * a chat turn — a runner without that capability would just leave the turn
 * QUEUED forever if picked. `RunnerOut.capabilities` is a caller-reported
 * bag of unknowns, so this checks it defensively rather than trusting the
 * shape.
 */
export function isSessionCapable(runner: Pick<RunnerOut, "capabilities">): boolean {
  return runner.capabilities?.sessions === true;
}

/** Online + session-capable runners from the fleet — the eligible set for
 * directed placement (new-chat "Run on" for a project chat, and the offline
 * banner's "Continue on…"). */
export function onlineSessionCapableRunners(
  fleet: readonly RunnerOut[],
): RunnerOut[] {
  return fleet.filter((r) => r.status === "online" && isSessionCapable(r));
}

/**
 * Whether the session's bound runner is offline — i.e. whether to raise the
 * placement banner and let the user choose wait-vs-continue.
 *
 * `runnerOnline` (SessionOut.runner_online) is AUTHORITATIVE when present: the
 * server computed it from the actual binding, so it needs no name matching and
 * no fleet fetch. The fleet list is only a fallback for payloads without it.
 *
 * The fallback distinguishes two things the old "no match ⇒ false" conflated:
 *   * an EMPTY fleet is genuinely unknown (not loaded, or the request failed) —
 *     still fail quiet, since alarming on missing data is worse than silence;
 *   * a LOADED fleet that does not contain the runner is evidence, not absence.
 *     `GET /runners/` omits retired runners, so a bound-but-unlisted runner is
 *     one that can never claim again. Failing quiet there is what left a send
 *     queued forever with no banner and no way to place it (labs 2026-07-25:
 *     10 sessions bound to a retired jj-mbp-cdp).
 */
/**
 * The liveness half of a session payload — the two fields `_out` computes from
 * the binding's runner. Structural rather than the whole SessionOut so these
 * helpers stay testable from a two-key literal.
 */
export type SessionRunnerLiveness = {
  runner_online?: boolean | null
  runner_status?: string | null
}

/**
 * Why a session's runner cannot take a message, or null when it can.
 *
 * `paused` and `offline` read the same to the server (both are "not ONLINE", so
 * `claim_next_turn` refuses either) but mean opposite things to a human: a pause
 * is a decision someone made and can undo from the Runners tab, while offline is
 * a box to go look at. Naming only the bool would collapse them.
 */
export function parkedReason(session: SessionRunnerLiveness): 'paused' | 'offline' | null {
  if (session.runner_online !== false) return null   // null/undefined = unbound, nothing to be offline
  return session.runner_status === 'paused' ? 'paused' : 'offline'
}

/**
 * Split a session list into the ones whose runner can act and the ones parked
 * behind a paused/offline runner. UNBOUND sessions count as live: a web chat
 * that has never sent has no runner yet and gets one when it does — hiding it
 * would make a brand-new chat vanish the moment you created it.
 */
export function partitionByRunnerReachability<T extends SessionRunnerLiveness>(
  sessions: readonly T[],
): { live: T[]; parked: T[] } {
  const live: T[] = []
  const parked: T[] = []
  for (const s of sessions) (parkedReason(s) ? parked : live).push(s)
  return { live, parked }
}

/** One line explaining what the default list is holding back — named by reason
 * when they agree, generic when a pause and a dead box are both in there. */
export function parkedSummary(parked: readonly SessionRunnerLiveness[]): string {
  if (parked.length === 0) return ''
  const reasons = new Set(parked.map(parkedReason))
  const why = reasons.size === 1 ? `runner ${[...reasons][0]}` : 'runner unavailable'
  return `${parked.length} hidden — ${why}`
}

/**
 * The fleet row for a session's bound runner, or null.
 *
 * The session payload carries the runner's NAME but not its id, and acting on a
 * runner (resume) needs the id — so the name is matched against the fleet list,
 * the same join `isBoundRunnerOffline` falls back to. Null when the fleet hasn't
 * loaded, when the name is absent, or when the runner isn't in the caller's
 * fleet at all (retired, or paired by someone else) — every one of which means
 * "you cannot act on it from here", which is what the caller does with this.
 */
export function findBoundRunner<T extends { name: string }>(
  runnerName: string | null | undefined,
  fleet: readonly T[],
): T | null {
  if (!runnerName) return null
  return fleet.find((r) => r.name === runnerName) ?? null
}

export function isBoundRunnerOffline(
  runnerName: string | null | undefined,
  fleet: readonly Pick<RunnerOut, "name" | "status">[],
  runnerOnline?: boolean | null,
): boolean {
  if (!runnerName) return false;
  if (runnerOnline === false) return true;
  if (runnerOnline === true) return false;
  const match = fleet.find((r) => r.name === runnerName);
  if (!match) return fleet.length > 0;
  return match.status !== "online";
}
