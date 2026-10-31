import { useMemo } from "react";

import type { Message, SessionState, TurnStatus } from "./protocol";
import { heldSendClientIds, isHeldSend } from "./placeSends";
import { startedRunningAgent } from "./subagents";
import { turnPhase, type TurnPhase } from "./turnStatus";
import { isPersonMessage } from "./turnWork";

/**
 * "Is the last turn live", answered ONCE for every surface that shows it: the
 * host's header chip, the panel's activity line and Stop, and whether the
 * transcript folds the last turn's work. Each of those reads this rather than
 * deriving its own, so a header can never say "running" over a panel that has
 * folded the turn, or the reverse.
 */

/**
 * The reply still streaming in, or null.
 *
 * Only the NEWEST assistant row can be in flight: a turn produces its rows in
 * order, so an older row still marked streaming is one whose
 * `chat.stream_complete` never arrived, and it says nothing about now.
 *
 * And even the newest is ignored once the server has settled the turn after
 * that row began (`finished_at >= created_at`): the server's status is the
 * record, and a frame lost on the way must not hold "working" up until the
 * next reconnect. A streaming row newer than the settled turn is work typed
 * straight into the session, which makes no Turn — that one is live.
 */
export function inFlightRow(
  messages: readonly Message[],
  status: TurnStatus | null | undefined,
): Message | null {
  let row: Message | null = null;
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === "assistant") {
      row = messages[i];
      break;
    }
  }
  if (!row || (row.status !== "streaming" && row.status !== "pending")) return null;
  if (status?.settled && status.finished_at) {
    const finished = Date.parse(status.finished_at);
    const began = Date.parse(row.created_at);
    if (!Number.isNaN(finished) && (Number.isNaN(began) || finished >= began)) return null;
  }
  return row;
}

export interface TurnView {
  /** Where the turn stands, in one word (`turnPhase`). */
  phase: TurnPhase;
  /** The reply streaming in now (`inFlightRow`). */
  inFlight: Message | null;
  /** A reply is coming: one is streaming, or the agent has the floor. */
  working: boolean;
  /** The last turn is still running, so its work shows as it happens rather
   *  than folded behind one line. */
  live: boolean;
  /** `live` for the transcript as drawn. A send of yours drawn in the queue is
   *  not in the transcript, so its last turn is the one before — live only if
   *  that turn itself is still running. */
  listLive: boolean;
  /** Stop is on offer: something is working and this viewer has a draft (a
   *  viewer, who cannot send, cannot stop either). */
  canStop: boolean;
  /** The phase as the header says it: "needs you", "running", "queued", "idle". */
  label: string;
  /** The box holding the turn — the server's `claimed_by`, else the fallback
   *  the host knows (the session's bound runner). */
  runner: string | null;
}

export interface TurnViewInput {
  state: SessionState;
  /** A send is outstanding with no reply yet (`useSessionSocket().awaitingReply`). */
  awaitingReply?: boolean;
  /** The person answered the agent's dialog here. Nothing fires on the way
   *  out of a wait, so `activity` reads "blocked" until the agent's next row;
   *  the answer is what ends the wait, so the turn reads as working. */
  pendingAnswer?: boolean;
  /** The runner to name when the server's status names none. */
  runnerFallback?: string | null;
}

const LABELS: Record<TurnPhase, string> = {
  blocked: "needs you",
  working: "running",
  queued: "queued",
  idle: "idle",
};

/** The last turn's `tool_use` rows — the calls that may have started a
 *  background agent which keeps the turn alive after its reply. */
function lastTurnToolUses(messages: readonly Message[]): Message[] {
  const out: Message[] = [];
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (isPersonMessage(m)) break;
    if (m.role === "tool_use") out.push(m);
  }
  return out;
}

export function turnView({
  state,
  awaitingReply = false,
  pendingAnswer = false,
  runnerFallback = null,
}: TurnViewInput): TurnView {
  const status = state.turn_status ?? null;
  const activity = state.activity;
  const inFlight = inFlightRow(state.messages, status);
  let phase = turnPhase({ status, activity, awaitingReply, replying: inFlight != null });
  // An answered dialog: the agent is resuming, whatever the hook last said.
  if (phase === "blocked" && pendingAnswer) phase = "working";
  const hasFloor = phase === "working" || phase === "queued";
  const working = inFlight != null || hasFloor;
  const background = startedRunningAgent(lastTurnToolUses(state.messages), state.subagents ?? []);
  const live =
    working ||
    background ||
    activity === "working" ||
    activity === "blocked" ||
    Boolean(status && (status.menu_pending || (!status.settled && !status.stuck)));
  const held = heldSendClientIds(state.queued, status);
  const sendInQueue = held.size > 0 && state.messages.some((m) => isHeldSend(m, held));
  const listLive = sendInQueue
    ? activity === "working" ||
      activity === "blocked" ||
      background ||
      Boolean(status?.menu_pending) ||
      (status?.state === "queued_behind" && status.behind !== "agent") ||
      inFlight != null
    : live;
  return {
    phase,
    inFlight,
    working,
    live,
    listLive,
    canStop: working && state.active_draft != null,
    label: LABELS[phase],
    runner: status?.claimed_by ?? runnerFallback ?? null,
  };
}

/** `turnView`, memoised on what it reads. */
export function useTurnView({
  state,
  awaitingReply = false,
  pendingAnswer = false,
  runnerFallback = null,
}: TurnViewInput): TurnView {
  return useMemo(
    () => turnView({ state, awaitingReply, pendingAnswer, runnerFallback }),
    [state, awaitingReply, pendingAnswer, runnerFallback],
  );
}
