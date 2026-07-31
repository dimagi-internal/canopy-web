/**
 * Pure helpers for ChatPage's REST<->kit wiring — split out of the component
 * so the conversion + the "Load full session" state machine unit-test without
 * mounting React or a WebSocket.
 */

import type { Message } from "canopy-ui/chat";
import type { ChatSessionDetail } from "@/api/chat";

/**
 * A REST `MessageOut` (turn_index/role/plaintext/content/created_at) -> the
 * kit's `Message` shape. Synthetic id (`t<turn_index>`) + `status: "complete"`
 * — `prependHistory` dedupes by `turn_index`, so a synthetic-id row never
 * collides with the WS row of the same index.
 */
export function restToKitMessage(
  m: ChatSessionDetail["messages"][number],
): Message {
  return {
    id: `t${m.turn_index}`,
    turn_index: m.turn_index,
    role: m.role as Message["role"],
    content: m.content,
    plaintext: m.plaintext,
    status: "complete",
    error_detail: null,
    started_at: null,
    completed_at: m.created_at,
    created_at: m.created_at,
  };
}

/**
 * What "Load full session" should do next, given a `BackfillStateOut.status`:
 * - `ready`       — the server already has the full transcript; reload now.
 * - `requested`   — the runner was just asked; give it a beat to land, then reload.
 * - `unavailable` — no runner to ask; show the offline/history-unavailable banner.
 * Any other/unknown status degrades to an immediate reload rather than silently
 * treating it as unavailable.
 */
export type BackfillAction = "reload-now" | "reload-after-delay" | "unavailable";

export function backfillAction(status: string): BackfillAction {
  if (status === "unavailable") return "unavailable";
  if (status === "requested") return "reload-after-delay";
  return "reload-now";
}

/**
 * Whether to offer "Load full session".
 *
 * A local (origin=runner) session's history lives on the runner, not the
 * server — until a backfill lands the server may hold ZERO `Message` rows. So
 * the offer must NOT depend on messages already being on screen: an empty
 * discovered session is precisely the case that needs it (found in prod: an
 * `origin=runner` session rendered "Start the conversation" with no way to pull
 * its transcript). Gate only on:
 *  - `hasMoreBefore` — the server holds more than the loaded window, so
 *    "Load earlier" is the right control instead;
 *  - `historyUnavailable` — we already asked and the runner wasn't reachable.
 * Clicking when the server happens to be complete is a harmless no-op (the
 * backend answers `ready` and we just reload the same rows).
 */
export function shouldShowLoadFull(args: {
  origin: string | null | undefined;
  hasMoreBefore: boolean;
  historyUnavailable: boolean;
}): boolean {
  return (
    args.origin === "runner" && !args.hasMoreBefore && !args.historyUnavailable
  );
}

/**
 * Why the composer refuses to send, or undefined when it may.
 *
 * A send into a runner that cannot act does NOT fail — `send_message` enqueues
 * it pinned to the session's binding and it sits QUEUED until that box returns,
 * which for a pause someone applied may be never. A queued message is
 * indistinguishable from a sent one until you notice no reply came, and nobody
 * types into a chat meaning to schedule it for whenever a laptop wakes up. So
 * the composer refuses up front and the placement banner holds the ways out
 * (resume it, or continue on another runner).
 *
 * Fails OPEN by construction: `boundOffline` is false whenever liveness is
 * merely UNKNOWN (no binding at all, or a fleet list that hasn't loaded), so an
 * unknown never locks the composer — the failure mode of over-blocking is a
 * chat you cannot use, which is worse than the queue this prevents.
 */
export function sendBlockReason(args: {
  runnerName: string | null | undefined;
  boundOffline: boolean;
  paused: boolean;
}): string | undefined {
  if (!args.boundOffline || !args.runnerName) return undefined;
  return args.paused
    ? `${args.runnerName} is paused — resume it to send`
    : `${args.runnerName} is unavailable — continue on another runner to send`;
}
