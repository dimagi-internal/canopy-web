import { Loader2 } from "lucide-react";

import { ThinkingIndicator } from "./MessageItem";
import { formatElapsed, type ActivityText } from "./turnWork";
import { useNow } from "./useNow";

export interface ActivityLineProps {
  /** A runner has the turn (`turn_status.state === "working"`, or the hook's
   *  `working` on a host with no status). False = still queued. */
  working: boolean;
  /** The kit's wording for a queued turn (`turnStatus.pendingLabel`). */
  queuedLabel?: string;
  /** Its newest call still running, or null while it thinks. */
  activity: ActivityText | null;
  /** When the turn started (ISO) — the clock counts from here. */
  startedAt?: string | null;
  /** The runner holding it. */
  runner?: string | null;
  /** Offer Stop here too. Omit for a viewer who cannot stop the agent. */
  onStop?: () => void;
  /** A stop has been asked for and not yet landed. */
  stopping?: boolean;
}

/**
 * The live line under a running turn: what it is doing NOW, how long it has
 * been at it, and how to stop it — Claude Code's "Running `npm test`… (1m 12s
 * · esc to interrupt)". The state itself (queued vs working) is the server's
 * `turn_status`; only the wording is here.
 */
export function ActivityLine({
  working,
  queuedLabel,
  activity,
  startedAt,
  runner,
  onStop,
  stopping = false,
}: ActivityLineProps) {
  const now = useNow(1000);
  const started = startedAt ? Date.parse(startedAt) : NaN;
  const elapsed = Number.isNaN(started) ? null : formatElapsed(now - started);

  return (
    <div
      data-testid="pending-reply"
      className="my-1 mr-auto flex max-w-full min-w-0 items-center gap-1.5 rounded-full border border-border bg-muted/60 px-3 py-1 text-xs text-muted-foreground"
    >
      {working ? (
        <>
          <Loader2 className="h-3 w-3 shrink-0 animate-spin text-primary" aria-hidden="true" />
          <span aria-live="polite" data-testid="activity-text" className="min-w-0 truncate text-foreground">
            {activity ? (
              <>
                {activity.verb}
                {activity.target && (
                  <>
                    {" "}
                    <code className="rounded bg-background px-1 font-mono text-[11px]">
                      {activity.target}
                    </code>
                  </>
                )}
              </>
            ) : (
              "Thinking…"
            )}
          </span>
        </>
      ) : (
        <span aria-live="polite" className="min-w-0 truncate">
          <ThinkingIndicator label={queuedLabel} />
        </span>
      )}
      {elapsed && (
        <span className="shrink-0 tabular-nums" data-testid="activity-elapsed" aria-hidden="true">
          · {elapsed}
        </span>
      )}
      {working && runner && <span className="shrink-0">· on {runner}</span>}
      {onStop &&
        (stopping ? (
          <span className="shrink-0">· stopping…</span>
        ) : (
          <span className="flex shrink-0 items-center gap-1">
            ·
            <button
              type="button"
              onClick={onStop}
              className="rounded px-1 font-medium text-foreground underline-offset-2 hover:underline"
              title="Stop the agent (Esc in the message box)"
            >
              Stop
            </button>
            <kbd className="rounded border border-border px-1 font-mono text-[10px]">esc</kbd>
          </span>
        ))}
    </div>
  );
}
