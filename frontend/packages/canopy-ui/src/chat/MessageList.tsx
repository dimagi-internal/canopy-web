import { useMemo, useState, type ReactNode } from "react";
import { ChevronRight, ChevronsDownUp, ChevronsUpDown, Loader2 } from "lucide-react";

import type { Message } from "./protocol";
import { Button } from "../ui/button";
import type { RenderMarkdown } from "./MessageItem";
import { MessageItem, ThinkingIndicator } from "./MessageItem";
import { ToolCallPair } from "./ToolCallPair";
import { pairToolMessages } from "./pairToolMessages";
import { groupToolRuns, runHasError, runIsActive, summariseRun } from "./groupToolRuns";

interface Props {
  messages: Message[];
  /** Rendered when there are no messages yet (replaces ace's WelcomePanel). */
  emptyState?: ReactNode;
  renderMarkdown?: RenderMarkdown;
  /**
   * The agent has the floor but has produced no row yet — render a trailing
   * "thinking" bubble at the bottom of the conversation.
   *
   * A trailing ELEMENT, not a row spliced into `messages`: the message array is
   * a projection of the durable record and must keep exactly one writer, and a
   * placeholder with a made-up id would need upsert/eviction rules against every
   * frame that can follow it. This has none — it is on screen while the flag is
   * true and gone when it isn't.
   */
  pendingReply?: boolean;
  /** Wording for that bubble — the caller knows whether the turn is still
   *  queued for a runner or the agent is actually working. */
  pendingLabel?: string;
}

// Show the bulk expand/collapse toolbar once a session has more than this
// many tool rows. Below that, the per-row toggle is enough.
const TOOLBAR_THRESHOLD = 5;

type BulkState = "default" | "all" | "none";

export function MessageList({
  messages,
  emptyState,
  renderMarkdown,
  pendingReply = false,
  pendingLabel,
}: Props) {
  const paired = useMemo(() => pairToolMessages(messages), [messages]);
  // Collapse back-to-back tool calls into one row. An agent mid-task emits long
  // stretches of them, and one row each pushes the prose you actually read off
  // the screen.
  const rows = useMemo(() => groupToolRuns(paired), [paired]);
  const toolPairCount = useMemo(
    () => paired.filter((r) => r.kind === "tool_pair").length,
    [paired],
  );
  // ``default`` = each <details> uses its own native state (collapsed
  // initially, user can toggle individually). The bulk toggles flip
  // every row open or closed at once. Reverting to "default" hands
  // control back to per-row state.
  const [bulkState, setBulkState] = useState<BulkState>("default");

  if (messages.length === 0 && !pendingReply) {
    return <>{emptyState ?? null}</>;
  }
  const forceToolOpen =
    bulkState === "all" ? true : bulkState === "none" ? false : undefined;

  return (
    <div className="flex flex-col">
      {toolPairCount > TOOLBAR_THRESHOLD && (
        <div className="sticky top-0 z-10 flex items-center justify-end gap-2 border-b border-border bg-background/80 px-4 py-1.5 backdrop-blur">
          <span className="text-xs text-muted-foreground">
            {toolPairCount} tool calls
          </span>
          <Button
            variant={bulkState === "all" ? "secondary" : "ghost"}
            size="xs"
            onClick={() => setBulkState(bulkState === "all" ? "default" : "all")}
            aria-pressed={bulkState === "all"}
          >
            <ChevronsUpDown className="h-3 w-3" />
            Expand all
          </Button>
          <Button
            variant={bulkState === "none" ? "secondary" : "ghost"}
            size="xs"
            onClick={() =>
              setBulkState(bulkState === "none" ? "default" : "none")
            }
            aria-pressed={bulkState === "none"}
          >
            <ChevronsDownUp className="h-3 w-3" />
            Collapse all
          </Button>
        </div>
      )}
      <div className="flex flex-col gap-2 p-4">
        {rows.map((row) => {
          if (row.kind === "tool_run") {
            // One line for a whole run, open on demand. `open` when the bulk
            // toggle says so, and always when something in it failed — a
            // collapsed group must never hide an error.
            const failed = runHasError(row.rows);
            // A run with a call still in flight must say so: collapsed, an agent
            // mid-task would otherwise look idle.
            const active = runIsActive(row.rows);
            return (
              <details
                key={row.key}
                open={forceToolOpen ?? (failed || undefined)}
                className="group my-1 rounded border border-border bg-muted/40 text-sm"
              >
                <summary className="flex cursor-pointer items-center gap-2 px-2 py-1.5 text-muted-foreground hover:bg-muted/60 select-none [&::-webkit-details-marker]:hidden">
                  <ChevronRight className="h-3 w-3 shrink-0 transition-transform group-open:rotate-90" />
                  <span className="text-xs font-medium text-foreground">
                    {summariseRun(row.rows)}
                  </span>
                  {active && (
                    <span className="flex items-center gap-1 text-xs text-muted-foreground">
                      <Loader2 className="h-3 w-3 animate-spin" /> running
                    </span>
                  )}
                  {failed && (
                    <span className="text-xs font-medium text-destructive">
                      · contains an error
                    </span>
                  )}
                </summary>
                <div className="space-y-1 border-t border-border/60 p-2">
                  {row.rows.map((r) =>
                    r.kind === "tool_pair" ? (
                      <ToolCallPair
                        key={r.key}
                        use={r.use}
                        result={r.result}
                        forceOpen={forceToolOpen}
                      />
                    ) : null,
                  )}
                </div>
              </details>
            );
          }
          if (row.kind === "tool_pair") {
            return (
              <ToolCallPair
                key={row.key}
                use={row.use}
                result={row.result}
                forceOpen={forceToolOpen}
              />
            );
          }
          return (
            <MessageItem
              key={row.key}
              message={row.message}
              forceToolOpen={forceToolOpen}
              renderMarkdown={renderMarkdown}
            />
          );
        })}
        {pendingReply && (
          <div
            data-testid="pending-reply"
            aria-live="polite"
            className="my-2 mr-auto max-w-[80%] rounded-2xl bg-muted px-4 py-2 text-foreground"
          >
            <ThinkingIndicator label={pendingLabel} />
          </div>
        )}
      </div>
    </div>
  );
}
