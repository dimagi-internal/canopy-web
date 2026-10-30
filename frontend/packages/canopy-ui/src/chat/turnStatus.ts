import type { TurnStatus } from "./protocol";

/**
 * The chat kit's VOICE for `harness.turn_status` — the counterpart of
 * `apps/slack/status.py::render`, which says the same thirteen states in Slack's
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
 *
 * Except that a settled status describes the LAST TURN, not the session: work
 * typed straight into the session (or a follow-up that rode the old turn)
 * makes no Turn, so the status stays the finished one while the hook says the
 * agent is working on the new prompt. The hook wins there.
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
    if (status.settled) return activity === "working";
    if (status.stuck) return false;
    // `picking_up`, `queued_behind` and `working` are the live states, and
    // each means a reply is genuinely coming.
    return true;
  }
  return Boolean(awaitingReply) || activity === "working";
}

/**
 * Where the conversation's turn stands, in one word — THE answer every surface
 * shows: the header chip, the pending bubble and Stop. They used to each derive
 * it (the header from `awaitingReply` alone, ignoring the server's status), so
 * one screen could say "queued" in the header over a turn the server had
 * settled, and a stale flag on either side lingered with nothing to correct it.
 *
 * `replying` is an assistant row streaming in now: the turn is plainly working
 * whatever the other sources say.
 */
export type TurnPhase = "blocked" | "working" | "queued" | "idle";

export function turnPhase({
  status,
  activity,
  awaitingReply,
  replying = false,
}: {
  status?: TurnStatus | null;
  activity?: "working" | "idle" | "blocked";
  awaitingReply?: boolean;
  replying?: boolean;
}): TurnPhase {
  if (activity === "blocked") return "blocked";
  if (replying) return "working";
  if (!agentHasFloor({ status, activity, awaitingReply })) return "idle";
  return status?.state === "working" || activity === "working" ? "working" : "queued";
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
  if (status?.state === "queued_behind") {
    // A live runner will take it, but only once the turn ahead ends — saying
    // "picking this up" made a follow-up sent mid-turn look lost (#1147).
    const [first] = status.runners;
    const ahead = status.behind === "session" ? "the previous message" : "the agent's current turn";
    return first ? `Queued behind ${ahead} on ${first}…` : `Queued behind ${ahead}…`;
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
 *
 * `latestMessageAt` is the newest message on screen. A settled turn's notice
 * is only true while that turn is the conversation's last word: work typed
 * straight into the session makes messages but no Turn, so without this a
 * day-old failure sat in red under a thread that was plainly working.
 */
/** A resume time as the reader's local "Oct 12, 3:00 PM". */
function formatWhen(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

/** How long a working turn may say nothing before the kit says so. Long enough
 *  that a model thinking through a hard step is not called out; short enough
 *  that a box which took the turn and went silent is not "Thinking…" for the
 *  rest of its lease. */
export const QUIET_AFTER_MS = 5 * 60 * 1000;

/** "6m" / "1h 5m" — a wait, not a timestamp. */
function waited(ms: number): string {
  const mins = Math.max(1, Math.round(ms / 60000));
  if (mins < 60) return `${mins}m`;
  const h = Math.floor(mins / 60);
  return mins % 60 ? `${h}h ${mins % 60}m` : `${h}h`;
}

/**
 * A working turn nothing has come back from in a while — in facts, not a verdict:
 * who took it, and for how long it has been quiet. On 2026-10-17 a box whose
 * token had been revoked claimed four turns and could report nothing; each one
 * read "Thinking on cloud-ec2-2…" until its lease ran out.
 *
 * "Back" is the newer of the server's `last_progress_at` (a ledger event) and the
 * newest message on screen, so a transcript row that streamed in counts too.
 */
function quietNotice(status: TurnStatus, latestMessageAt: string | null | undefined,
                     now: number): TurnNotice | null {
  if (status.state !== "working" || status.menu_pending || !status.claimed_at) return null;
  const claimed = Date.parse(status.claimed_at);
  if (Number.isNaN(claimed)) return null;
  let back = status.last_progress_at ? Date.parse(status.last_progress_at) : claimed;
  const seen = latestMessageAt ? Date.parse(latestMessageAt) : NaN;
  if (!Number.isNaN(seen) && seen > back) back = seen;
  if (now - back < QUIET_AFTER_MS) return null;
  const who = status.claimed_by ?? "its runner";
  return {
    tone: "warn",
    text: back <= claimed
      ? `${who} took this ${waited(now - claimed)} ago and nothing has come back from it since.`
      : `Nothing new from ${who} for ${waited(now - back)}.`,
    offer: null,
  };
}

export function turnNotice(
  status?: TurnStatus | null,
  { latestMessageAt, now = Date.now() }: { latestMessageAt?: string | null; now?: number } = {},
): TurnNotice | null {
  if (!status) return null;
  const quiet = quietNotice(status, latestMessageAt, now);
  if (quiet) return quiet;
  if (status.settled && status.finished_at && latestMessageAt
      && Date.parse(latestMessageAt) > Date.parse(status.finished_at)) {
    return null;
  }
  const offer = status.cloud_runner && status.cloud_runner_id
    ? { name: status.cloud_runner, id: status.cloud_runner_id }
    : null;
  const runners = status.runners.join(", ");
  const need = status.requires?.length ? status.requires.map((r) => r.replace(/_/g, "-")).join(", ") : "";
  switch (status.state) {
    case "waiting_runner":
      if (status.parked) {
        return {
          tone: "warn",
          // Paused is somebody's decision, not a laptop lid: say whether it ends
          // on its own.
          text: status.held_until
            ? `Queued — ${runners || "its runner"} is paused until ${formatWhen(status.held_until)}. It runs then.`
            : `Queued — ${runners || "its runner"} is paused, so this runs only when someone resumes it.`,
          offer,
        };
      }
      return {
        tone: "warn",
        // Names the box, because the fix is physical: go and open it.
        text: (runners
          ? `Queued — ${runners} is offline, so nothing is working on this yet. It runs when the runner is back.`
          : "Queued — its runner is offline, so nothing is working on this yet.")
          + (need ? ` It needs a ${need} runner.` : ""),
        offer,
      };
    case "signed_out":
      return {
        tone: "error",
        // Online is not the same as able: the box heartbeats, and every turn it
        // took came back as the CLI's "Not logged in". Waiting will not fix it.
        text: `Queued — Claude isn't signed in on ${runners || "its runner"}, so nothing will run this. `
          + "Sign in there (`claude /login`) and it picks this up.",
        offer,
      };
    case "unrouted":
      return {
        tone: "error",
        // The one state waiting cannot fix, and it has to READ differently
        // from the one waiting can.
        text: need
          ? `Queued — this conversation needs a ${need} runner, and none of ${status.agent_slug ?? "its agent"}'s runners is declared ${need}.`
          : status.agent_slug
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
      // The reason, when the runner gave one: "failed" covers an undelivered
      // message, a stop that did not take and a create that never rendered,
      // and each has a different fix — which the note usually spells out.
      return {
        tone: "error",
        text: status.detail ? `Could not finish — ${status.detail}` : "Could not finish.",
        offer: null,
      };
    case "cancelled":
      // Only a REJECTED turn carries a detail (an admission rule closed it before
      // it ran); one somebody stopped needs no sentence.
      return status.detail
        ? { tone: "error", text: `Not run — ${status.detail}`, offer: null }
        : null;
    case "missed":
      return { tone: "warn", text: "Missed — nothing picked it up in time.", offer: null };
    default:
      return null;
  }
}

/**
 * The composer's placeholder while the agent has the floor, or null for the
 * kit's default ("sent after the current reply").
 *
 * Says what will actually happen to the next message, from the server's
 * answer (`turn_status.midturn`): on a laptop runner a follow-up is typed into
 * the running session and the agent reads it mid-turn (canopy-web#1153);
 * elsewhere it waits, and the reason it waits decides the words.
 */
export function composerPlaceholder(status?: TurnStatus | null): string | null {
  if (!status) return null;
  // "After the current reply" is false while nothing is running: the ask
  // ahead is stuck, so a new message waits behind it.
  if (status.stuck && !status.menu_pending && !status.settled) {
    return "Type a message — it waits in the queue behind the one above";
  }
  if (status.state !== "working") return null;
  if (status.midturn) return "Message the agent — it reads this mid-turn";
  switch (status.midturn_blocked) {
    case "dialog":
      return "Type a message — it is sent once the question above is answered";
    case "runner":
      return `Type a message — ${status.claimed_by ?? "this runner"} can't take one mid-turn, so it is sent after the current reply`;
    default:
      return null;
  }
}
