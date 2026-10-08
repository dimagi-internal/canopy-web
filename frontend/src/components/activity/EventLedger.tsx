import { useEffect, useState } from "react";
import { listTurnEvents, type TurnEvent } from "@/api/turns";

/** Lazily loads the turn's event ledger on first expand; component unmounts on
 * collapse, so re-expanding refetches — fine for a rarely-opened drill-down. */
export function EventLedger({ turnId }: { turnId: string }) {
  const [events, setEvents] = useState<TurnEvent[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    listTurnEvents(turnId)
      .then((e) => { if (alive) setEvents(e); })
      .catch((e) => { if (alive) setError(e instanceof Error ? e.message : "Failed to load events"); });
    return () => { alive = false; };
  }, [turnId]);

  if (error) return <p className="text-xs text-destructive">{error}</p>;
  if (events === null) return <p className="text-xs text-muted-foreground">Loading events…</p>;
  if (events.length === 0) return <p className="text-xs text-muted-foreground">No events recorded.</p>;

  return (
    <ol className="space-y-1">
      {events.map((e) => (
        <li key={e.seq} className="flex gap-2 text-xs">
          <span className="text-foreground-subtle tabular-nums">#{e.seq}</span>
          <span className="text-foreground-secondary" title={new Date(e.ts).toLocaleString()}>
            {new Date(e.ts).toLocaleTimeString()}
          </span>
          <span className="font-medium text-foreground">{e.kind}</span>
          {e.kind === "unproven_member" && (
            // Logged by canopy, not the runner (canopy-web#1265): a member's mail
            // that could not be tied to them. Informational; it grants nothing.
            <span className="text-foreground-secondary">
              {String(e.payload?.email ?? "")}: {String(e.payload?.note ?? "")}
            </span>
          )}
          {e.kind === "sender_trust" && (
            // Logged by canopy: an unaligned member email let in by a temporary
            // trust rule an agent admin set (agents.models.SenderTrust).
            <span className="text-foreground-secondary">
              {String(e.payload?.email ?? "")}: trusted until{" "}
              {new Date(String(e.payload?.expires_at ?? "")).toLocaleDateString()} ({String(e.payload?.reason ?? "")})
            </span>
          )}
        </li>
      ))}
    </ol>
  );
}
