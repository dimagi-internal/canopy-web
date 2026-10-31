import type { Message, QueuedMessage, TurnStatus } from "./protocol";
import { isMine, isUnconfirmed } from "./identity";

/**
 * Where each of your sends is drawn: in the transcript as your own bubble, or
 * in the queue below it. Pure, so the panel's four answers about sends — which
 * rows leave the transcript, which queued entries are already on screen, which
 * rows carry a Cancel, and which texts are your own unconfirmed lines — come
 * from one place and cannot disagree.
 */

/** Turn states in which a fresh send sits and waits rather than being picked
 *  up in a moment: behind a running turn, or with no box that will take it. */
export const HELD_STATES: ReadonlySet<string> = new Set([
  "queued_behind", "waiting_runner", "signed_out", "unrouted", "working", "blocked", "paused",
]);

/** The client ids of sends that wait in the queue rather than as a bubble.
 *
 *  A `midturn` send always does. A `queued` one does when something holds it:
 *  a send ahead of it, or a status that says it will wait. The head of the
 *  queue on an idle box is claimed within a second or two, so it keeps its
 *  bubble — moving it out for that window and back reads as a flicker. */
export function heldSendClientIds(
  queued: readonly QueuedMessage[] | undefined,
  status: TurnStatus | null | undefined,
): Set<string> {
  const waitIsReal = status != null && HELD_STATES.has(status.state);
  return new Set(
    (queued ?? [])
      .filter((q, i) => q.client_id && (q.state === "midturn" || (q.state === "queued" && (i > 0 || waitIsReal))))
      .map((q) => q.client_id as string),
  );
}

/** Your own optimistic row for a send the queue draws instead. */
export function isHeldSend(m: Message, held: ReadonlySet<string>): boolean {
  return (
    m.role === "user" &&
    isUnconfirmed(m) &&
    typeof m.content?.client_id === "string" &&
    held.has(m.content.client_id)
  );
}

export interface SendPlacement {
  /** The transcript's rows, without your sends the queue draws. */
  shown: Message[];
  /** At least one of your sends is drawn in the queue, not the transcript. */
  sendInQueue: boolean;
  /** Every client id still on screen in the transcript: the queue skips
   *  those entries, so a send never shows twice. */
  hideClientIds: Set<string>;
  /** Your own still-queued sends, client id → turn id: their bubbles carry
   *  a Cancel. */
  cancellable: Map<string, string>;
  /** The text of your own unconfirmed rows, for a queued entry that carries
   *  no client id (an older host's HTTP send). */
  ownPendingTexts: Set<string>;
}

export function placeSends(
  messages: Message[],
  queued: readonly QueuedMessage[] | undefined,
  me: { userId: number | null; contactId: number | null },
  status: TurnStatus | null | undefined,
): SendPlacement {
  const held = heldSendClientIds(queued, status);
  const shown = held.size === 0 ? messages : messages.filter((m) => !isHeldSend(m, held));
  const hideClientIds = new Set(
    shown.map((m) => m.content?.client_id).filter((c): c is string => typeof c === "string"),
  );
  const cancellable = new Map(
    (queued ?? [])
      .filter((q) => q.state === "queued" && q.client_id && isMine(q.author, me.userId, me.contactId))
      .map((q) => [q.client_id as string, q.turn_id] as const),
  );
  const ownPendingTexts = new Set(
    shown
      .filter(
        (m) =>
          m.role === "user" &&
          (m.status === "pending" || m.id.startsWith("local:")) &&
          (!m.author || isMine(m.author, me.userId, me.contactId)),
      )
      .map((m) => m.plaintext.trim()),
  );
  return {
    shown,
    sendInQueue: shown.length !== messages.length,
    hideClientIds,
    cancellable,
    ownPendingTexts,
  };
}
