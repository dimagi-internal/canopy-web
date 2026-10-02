import { useEffect, useState } from "react";
import { listTurnMessages, type TurnMessages } from "@/api/turns";

/** What a turn did, read from its retained transcript. Used for turns with no
 * chat behind them — a cloud runner runs an agent turn as one-shot `claude -p`,
 * and this transcript is the only record of the work. Loads on mount; the card
 * mounts it only when opened. */
export function TurnTranscript({ turnId }: { turnId: string }) {
  const [data, setData] = useState<TurnMessages | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    listTurnMessages(turnId)
      .then((d) => { if (alive) setData(d); })
      .catch((e) => { if (alive) setError(e instanceof Error ? e.message : "Failed to load the transcript"); });
    return () => { alive = false; };
  }, [turnId]);

  if (error) return <p className="text-xs text-destructive">{error}</p>;
  if (data === null) return <p className="text-xs text-muted-foreground">Loading transcript…</p>;
  if (data.messages.length === 0) return <p className="text-xs text-muted-foreground">The transcript is empty.</p>;

  return (
    <div className="space-y-2 max-h-[32rem] overflow-y-auto">
      {data.messages.map((m) => {
        if (m.role === "tool_use" || m.role === "tool_result") {
          return (
            <details key={m.turn_index} className="text-[11px] text-muted-foreground">
              <summary className="cursor-pointer">
                {m.role === "tool_use" ? m.plaintext : "Tool result"}
              </summary>
              <pre className="mt-1 whitespace-pre-wrap break-words text-foreground-secondary">
                {m.role === "tool_use" ? JSON.stringify(m.content, null, 2) : m.plaintext}
              </pre>
            </details>
          );
        }
        const isUser = m.role === "user";
        return (
          <div
            key={m.turn_index}
            className={`rounded-lg border border-border px-3 py-2 text-[12px] whitespace-pre-wrap break-words ${
              isUser ? "bg-muted text-foreground" : "bg-card text-foreground-secondary"
            }`}
          >
            <span className="block text-[10px] uppercase tracking-wide text-muted-foreground mb-0.5">
              {isUser ? "Prompt" : "Agent"}
            </span>
            {m.plaintext}
          </div>
        );
      })}
      {data.truncated && (
        <p className="text-[11px] text-muted-foreground">
          Showing the start of a long transcript.
        </p>
      )}
    </div>
  );
}
