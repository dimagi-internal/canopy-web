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
 * Whether the session's bound runner (by name — the session payload carries
 * no runner id, only `runner_name`) is offline, per the fleet runners list.
 * A name with no fleet match is NOT treated as offline — that's an unknown,
 * not evidence, and the banner should fail quiet rather than alarm on stale
 * or not-yet-loaded fleet data.
 */
export function isBoundRunnerOffline(
  runnerName: string | null | undefined,
  fleet: readonly Pick<RunnerOut, "name" | "status">[],
): boolean {
  if (!runnerName) return false;
  const match = fleet.find((r) => r.name === runnerName);
  if (!match) return false;
  return match.status !== "online";
}
