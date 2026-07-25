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
