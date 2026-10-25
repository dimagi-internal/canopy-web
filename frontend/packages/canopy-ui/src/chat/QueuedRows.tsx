import type { QueuedMessage } from "./protocol";
import { isMine } from "./identity";
import { authorColor } from "./personColor";

/** Sends that have not reached the agent yet, in send order, for everyone.
 *  Your own are skipped when your optimistic echo already shows them — by
 *  client_id (`hideClientIds`), or, for a client that never sent one (an older
 *  host's HTTP send), by the text of your own still-unconfirmed row
 *  (`ownPendingTexts`) — so a line never appears twice. */
export function QueuedRows({
  queued, hideClientIds, currentUserId, currentContactId, ownPendingTexts, onCancel,
}: {
  queued: QueuedMessage[];
  hideClientIds: Set<string>;
  currentUserId: number | null;
  currentContactId?: number | null;
  /** Trimmed text of the viewer's own optimistic rows not yet confirmed. */
  ownPendingTexts?: Set<string>;
  /** Withdraw one of your own still-queued sends; omit to offer no Cancel. */
  onCancel?: (turnId: string) => void;
}) {
  const rows = queued.filter((q) => {
    if (q.client_id && hideClientIds.has(q.client_id)) return false;
    const mine = isMine(q.author, currentUserId, currentContactId);
    return !(mine && ownPendingTexts?.has(q.text.trim()));
  });
  if (rows.length === 0) return null;
  return (
    <div className="px-3">
      {rows.map((q) => {
        const mine = isMine(q.author, currentUserId, currentContactId);
        return (
          <div key={q.turn_id} data-testid="queued-row"
               className={`my-2 max-w-[80%] rounded-2xl border border-dashed border-border px-4 py-2 text-sm ${mine ? "ml-auto" : "mr-auto"}`}>
            <div className="mb-0.5 flex gap-2 text-[11px] text-muted-foreground">
              <span
                className={`font-medium ${
                  !mine && q.author ? authorColor(q.author).text : "text-foreground"
                }`}
              >
                {mine ? "You" : q.author?.name ?? "Someone"}
              </span>
              <span data-testid="queued-state">{queuedLabel(q.state)}</span>
              {mine && q.state === "queued" && onCancel && (
                <button type="button" onClick={() => onCancel(q.turn_id)}
                        data-testid="queued-cancel"
                        className="ml-auto underline hover:text-foreground">
                  Cancel
                </button>
              )}
            </div>
            <div className="whitespace-pre-wrap text-foreground-secondary [overflow-wrap:anywhere]">{q.text}</div>
          </div>
        );
      })}
    </div>
  );
}

/** What has happened to a send so far. `midturn` is a follow-up typed into the
 *  agent's running turn: delivered, not waiting — the agent reads it mid-turn. */
export function queuedLabel(state: QueuedMessage["state"]): string {
  if (state === "queued") return "queued";
  if (state === "midturn") return "delivered into the current turn";
  return "sending to agent…";
}
