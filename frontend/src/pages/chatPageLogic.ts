/**
 * Pure helpers for ChatPage's REST<->kit wiring — split out of the component
 * so the conversion + the "Load full session" state machine unit-test without
 * mounting React or a WebSocket.
 */

import { restToKitMessage, type SessionMenu } from "canopy-ui/chat";

/**
 * Re-exported from `canopy-ui/chat`, which now owns it.
 *
 * It was written out here AND in ace-web, against this kit's own `Message`
 * type — so the kit was always its right home. Kept as a re-export rather than
 * asking every caller to change its import: the function has not moved as far
 * as this module's consumers are concerned.
 */
export { restToKitMessage };

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
 * Gated on the session having a RUNNER, not on `origin === "runner"`. Where a
 * conversation started says nothing about where its record lives: since
 * `transcript_sourced` (spec 2026-07-24) a phone-started chat is driven by the
 * same runner writing the same transcript, so its history is recoverable in
 * exactly the same way — but `origin` is `"web"`, so the control was hidden on
 * precisely the sessions started from the phone. Confirmed live on labs
 * (2026-07-31): an `origin="web"` session bound to an online runner, holding 0
 * of its 75 transcript rows, could not offer it.
 *
 * The offer must NOT depend on messages already being on screen — an empty
 * discovered session is the case that most needs it. Gate only on:
 *  - `runnerName` — no runner means no transcript to recover from;
 *  - `hasMoreBefore` — the server holds more than the loaded window, so
 *    "Load earlier" is the right control instead;
 *  - `historyUnavailable` — we already asked and the runner wasn't reachable.
 * Clicking when the server is already complete is a harmless no-op (the backend
 * answers `ready` and we reload the same rows).
 */
export function shouldShowLoadFull(args: {
  runnerName: string | null | undefined;
  hasMoreBefore: boolean;
  historyUnavailable: boolean;
}): boolean {
  return (
    Boolean(args.runnerName) && !args.hasMoreBefore && !args.historyUnavailable
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
/**
 * Whether this menu is a DIALOG the composer is drawn behind, rather than a
 * bare "somebody is wanted here" marker.
 *
 * The composer lock below rests entirely on the TUI drawing a dialog where the
 * input line would be. That is true of a parsed dialog — it has options,
 * because parsing one is what produced them. It is NOT true of an option-less
 * `Notification` marker: nothing was parsed there, nobody looked at the screen,
 * and the message may just be Claude Code's ordinary sixty-second idle nudge.
 *
 * Locking on those is how a chat becomes unusable with no way out. Measured
 * 2026-08-17: an API 500 ended a turn without firing `Stop`, the next idle
 * notification was recorded as a block, and the session showed a "Waiting on
 * you" with no options to pick, over a composer that refused to send — so the
 * only control left was a button labelled Cancel. The runner half of that bug
 * is fixed too, but this is the half that decides whether the next unforeseen
 * false marker is an inconvenience or a trap.
 *
 * Letting the send through costs nothing when the marker IS real: the runner
 * re-reads the screen, refuses with `COMPOSER_NOT_VISIBLE`, and ships back the
 * dialog it actually found. A loud bounce beats a silent lock.
 */
export function menuBlocksComposer(
  menu: { options?: unknown[] | null; questions?: unknown[] | null } | null | undefined,
): boolean {
  if (!menu) return false;
  return (menu.options?.length ?? 0) > 0 || (menu.questions?.length ?? 0) > 0;
}

export function sendBlockReason(args: {
  runnerName: string | null | undefined;
  boundOffline: boolean;
  paused: boolean;
  blockedOnMenu?: boolean;
}): string | undefined {
  // A dialog is up, so there is no prompt to send INTO: Claude Code's TUI draws
  // the menu where the composer would be, which is exactly the state the runner
  // reports as COMPOSER_NOT_VISIBLE and refuses to blind-send against. The send
  // would bounce, and the answer the agent is actually waiting for is one tap
  // away in the banner above. Checked before liveness because it is the more
  // specific fact — a dialog is up whether or not the box is also parked.
  //
  // Only a dialog, though — see `menuBlocksComposer`, which is what the caller
  // passes. An option-less marker leaves the composer alone.
  if (args.blockedOnMenu) return "answer the question above to continue";
  if (!args.boundOffline || !args.runnerName) return undefined;
  return args.paused
    ? `${args.runnerName} is paused — resume it to send`
    : `${args.runnerName} is unavailable — continue on another runner to send`;
}

/**
 * A tap on the "Waiting on you" dialog that the server has relayed (or is
 * relaying) to the runner.
 *
 * Nothing on the server clears the menu when you answer it: the runner presses
 * the key, and the retraction only arrives when the next session report notices
 * the dialog is gone (~10s) or the agent streams its next row. On a phone that
 * gap read as the tap being ignored. The buttons came back to life the moment
 * the POST returned, the header still said "needs you", and the composer stayed
 * locked, even though the answer had landed (Jonathan, 2026-09-18). So the page
 * hides the dialog it just answered, straight away, and only brings it back
 * when there is evidence the answer did NOT take.
 */
export interface PendingAnswer {
  /** `menuIdentity` of the dialog that was answered. */
  key: string;
  /** The `answer_note` the dialog carried at tap time. A DIFFERENT note arriving
   *  afterwards is the runner refusing this tap. */
  note: string;
  /** `activity` at tap time. Only a change away from it is news. */
  activity: string | undefined;
  at: number;
}

/** How long an answered dialog stays hidden while the producer still reports
 *  it. The session report re-derives the menu every ~10s, so a dialog still
 *  there after three reports did not take the key, and hiding it any longer
 *  would strand the agent behind a menu nobody can see. */
export const ANSWER_GRACE_MS = 30_000;

/** What makes two menu objects the same dialog. `observed_at` and the answer
 *  fields change on every report of the SAME dialog, so they are left out. */
export function menuIdentity(menu: SessionMenu): string {
  return JSON.stringify([
    menu.title ?? "",
    menu.question ?? "",
    menu.body ?? "",
    (menu.options ?? []).map((o) => [o.number, o.label]),
    (menu.questions ?? []).map((q) => [q.question, q.options.map((o) => [o.number, o.label])]),
  ]);
}

/** Whether the dialog on screen is the one we just answered, and should stay
 *  hidden. It comes back when the runner refused the tap (a new
 *  `answer_note`), when it is a different dialog, or when the grace window
 *  has run out with the dialog still reported. */
export function answerHidesMenu(
  menu: SessionMenu,
  pending: PendingAnswer | null,
  now: number,
): boolean {
  if (!pending) return false;
  if ((menu.answer_note ?? "") !== pending.note) return false;
  if (menuIdentity(menu) !== pending.key) return false;
  return now - pending.at < ANSWER_GRACE_MS;
}

export type ShareMode = "broadcast" | "bind";

/**
 * The line the chat page sends to have the SESSION summarize itself into
 * Slack. It is the canopy plugin's own skill invocation — the session writes
 * the summary because it has the context; canopy-web only carries the ask.
 * Returns null when the channel is not something Slack could name.
 */
export function shareToSlackCommand(channel: string, mode: ShareMode): string | null {
  const raw = channel.trim().replace(/^#/, "");
  // Slack channel names: lowercase letters, digits, - and _, up to 80; ids are
  // C…/G… uppercase alphanumerics. Anything else would reach the skill as prose.
  if (!/^[a-z0-9_-]{1,80}$/.test(raw) && !/^[CG][A-Z0-9]{6,}$/.test(raw)) return null;
  const target = /^[CG][A-Z0-9]{6,}$/.test(raw) ? raw : `#${raw}`;
  return `/canopy:share-to-slack ${target}${mode === "bind" ? " bind" : ""}`;
}
