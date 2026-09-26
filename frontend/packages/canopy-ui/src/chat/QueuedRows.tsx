import type { QueuedMessage } from "./protocol";

/** Sends that have not reached the agent yet, in send order, for everyone.
 *  Your own are skipped when your optimistic echo already shows them
 *  (`hideClientIds`), so a line never appears twice. */
export function QueuedRows({ queued, hideClientIds, currentUserId }: {
  queued: QueuedMessage[]; hideClientIds: Set<string>; currentUserId: number;
}) {
  const rows = queued.filter((q) => !q.client_id || !hideClientIds.has(q.client_id));
  if (rows.length === 0) return null;
  return (
    <div className="px-3">
      {rows.map((q) => {
        const mine = q.author?.user_id === currentUserId;
        return (
          <div key={q.turn_id} data-testid="queued-row"
               className={`my-2 max-w-[80%] rounded-2xl border border-dashed border-border px-4 py-2 text-sm ${mine ? "ml-auto" : "mr-auto"}`}>
            <div className="mb-0.5 flex gap-2 text-[11px] text-muted-foreground">
              <span className="font-medium text-foreground">{mine ? "You" : q.author?.name ?? "Someone"}</span>
              <span>{q.state === "queued" ? "queued" : "sending to agent…"}</span>
            </div>
            <div className="whitespace-pre-wrap text-foreground-secondary [overflow-wrap:anywhere]">{q.text}</div>
          </div>
        );
      })}
    </div>
  );
}
