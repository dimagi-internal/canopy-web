import type { TurnStatus } from "./protocol";

/**
 * The chat kit's VOICE for `harness.turn_status` — the counterpart of
 * `apps/slack/status.py::render`, which says the same eleven states in Slack's
 * words. The states are computed once on the server; only the wording is here.
 *
 * Pure functions, no React, so both the wording and the spinner-vs-statement
 * decision are unit-testable without mounting a panel.
 */

/**
 * Does the agent have the floor — i.e. should a "working on it" bubble show?
 *
 * Three sources, in order of authority:
 *
 *  1. `status` is the server's answer and wins whenever it exists. It is the
 *     only one that knows a turn is queued behind an offline box, and the only
 *     one that survives a reload.
 *  2. `awaitingReply` is client-side and answers INSTANTLY, with no round trip.
 *     Nothing server-side can: the turn has to be enqueued before anything
 *     could report on it. It covers the gap between pressing send and the
 *     first frame coming back.
 *  3. `activity` is the runner's hook clock, which keeps the bubble up across
 *     the gaps between an assistant's separate text blocks.
 *
 * A `blocked` agent never has the floor: one waiting on YOU must not render as
 * one working. Neither does a stuck or settled turn — a spinner over an ask
 * nothing is going to pick up is the exact lie this whole projection exists to
 * stop.
 */
export function agentHasFloor({
  status,
  activity,
  awaitingReply,
}: {
  status?: TurnStatus | null;
  activity?: "working" | "idle" | "blocked";
  awaitingReply?: boolean;
}): boolean {
  if (activity === "blocked") return false;
  if (status) {
    if (status.settled || status.stuck) return false;
    // `picking_up` and `working` are the two live states, and both mean a
    // reply is genuinely coming.
    return true;
  }
  return Boolean(awaitingReply) || activity === "working";
}

/**
 * The label inside the pending bubble.
 *
 * "Queued" vs "Thinking" is the useful distinction — where the delay IS, not
 * that there is one — and with a status in hand it can name the box, which
 * turns a spinner into something you can act on when it lasts too long.
 */
export function pendingLabel({
  status,
  activity,
}: {
  status?: TurnStatus | null;
  activity?: "working" | "idle" | "blocked";
}): string {
  if (status?.state === "working") {
    return status.claimed_by ? `Thinking on ${status.claimed_by}…` : "Thinking…";
  }
  if (status?.state === "picking_up") {
    const [first] = status.runners;
    if (status.pinned && first) return `Sent to ${first}…`;
    return first ? `Picking this up on ${first}…` : "Queued…";
  }
  return activity === "working" ? "Thinking…" : "Queued…";
}

export interface TurnNotice {
  /** `warn` — nothing is moving but waiting may still resolve it.
   *  `error`  — waiting will never resolve it; something has to change. */
  tone: "warn" | "error";
  text: string;
  /** The runner this could be moved to, when moving it would help. The kit
   *  only reports the offer; acting on it belongs to the host, which knows
   *  whether this user may place a turn. */
  offer: { name: string; id: string } | null;
}

/**
 * The sentence for a turn that is going nowhere, or null when there is nothing
 * a person needs to know.
 *
 * Only the states a spinner would MISREPRESENT get one. `picking_up` and
 * `working` are covered by the bubble; the terminal states are covered by the
 * reply (or its absence) already being on screen.
 *
 * `blocked` deliberately returns null: the dialog itself is the notice, and
 * the host renders it as a menu with buttons. A sentence beside it would say
 * the same thing twice and compete with the thing you can actually press.
 */
export function turnNotice(status?: TurnStatus | null): TurnNotice | null {
  if (!status) return null;
  const offer = status.cloud_runner && status.cloud_runner_id
    ? { name: status.cloud_runner, id: status.cloud_runner_id }
    : null;
  const runners = status.runners.join(", ");
  switch (status.state) {
    case "waiting_runner":
      return {
        tone: "warn",
        // Names the box, because the fix is physical: go and open it.
        text: runners
          ? `Queued — ${runners} is offline, so nothing is working on this yet. It runs when the runner is back.`
          : "Queued — its runner is offline, so nothing is working on this yet.",
        offer,
      };
    case "unrouted":
      return {
        tone: "error",
        // The one state waiting cannot fix, and it has to READ differently
        // from the one waiting can.
        text: status.agent_slug
          ? `Queued, but no runner is set up to run ${status.agent_slug} — nothing will pick this up until its routing is fixed.`
          : "Queued, but no runner is set up to run this — nothing will pick it up until its routing is fixed.",
        offer,
      };
    case "paused":
      return {
        tone: "warn",
        text: status.claimed_by
          ? `Paused — ${status.claimed_by} went offline mid-turn. It carries on if the runner comes back.`
          : "Paused — the runner went offline mid-turn.",
        offer,
      };
    case "lost":
      return {
        tone: "error",
        text: status.claimed_by
          ? `Could not finish — ${status.claimed_by} went away before it was done.`
          : "Could not finish — the runner went away before it was done.",
        offer,
      };
    case "failed":
      return { tone: "error", text: "Could not finish.", offer: null };
    case "missed":
      return { tone: "warn", text: "Missed — nothing picked it up in time.", offer: null };
    default:
      return null;
  }
}
