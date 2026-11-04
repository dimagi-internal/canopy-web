import { useEffect, useState } from "react";
import { listTurnEvents, type TurnEvent } from "@/api/turns";

/** What a plain job printed, as a plain log, and how it ended. A job runs no model, so its
 * turn has no transcript and no chat: its output arrives as `status` events carrying
 * `{source: "job", text}` (the box streams them), and `result_note` holds the exit and the
 * last lines. Nothing here asks for a transcript or offers a reply box. */
export function JobLog({ turnId, resultNote }: { turnId: string; resultNote: string }) {
  const [lines, setLines] = useState<string[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    listTurnEvents(turnId)
      .then((events: TurnEvent[]) => {
        if (!alive) return;
        setLines(
          events
            .filter((e) => e.kind === "status" && e.payload?.source === "job")
            .sort((a, b) => a.seq - b.seq)
            .map((e) => String(e.payload?.text ?? "")),
        );
      })
      .catch((e) => { if (alive) setError(e instanceof Error ? e.message : "Failed to load the output"); });
    return () => { alive = false; };
  }, [turnId]);

  const note = (resultNote ?? "").trim();
  return (
    <div className="space-y-2">
      {error ? (
        <p className="text-xs text-destructive">{error}</p>
      ) : lines === null ? (
        <p className="text-xs text-muted-foreground">Loading output…</p>
      ) : lines.length === 0 ? (
        <p className="text-xs text-muted-foreground">The job printed nothing.</p>
      ) : (
        <pre
          data-testid="job-log"
          className="max-h-[32rem] overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-muted p-3 font-mono text-[12px] text-foreground"
        >
          {lines.join("\n")}
        </pre>
      )}
      {note && (
        <pre className="whitespace-pre-wrap break-words font-mono text-[12px] text-foreground-secondary">{note}</pre>
      )}
    </div>
  );
}
