import { Check, ChevronRight, Loader2, X } from "lucide-react";

import type { Message } from "./protocol";
import {
  deriveToolStatus,
  toolDiffStats,
  toolDisplayName,
  toolPreview,
} from "./pairToolMessages";
import { subagentProgress, useRowSubagent } from "./subagents";
import { ToolBody } from "./toolBodies/ToolBody";

interface Props {
  use: Message;
  result: Message | null;
  /** Controlled-open state: true to force open, false to force closed,
   *  undefined to let the user control via the native <details> toggle. */
  forceOpen?: boolean;
}

/**
 * Renders a tool_use + tool_result as a single collapsible row.
 *
 * Header line: ``{status icon} {tool name} · {preview}`` — readable when
 * collapsed so the user can scan a long stream of tool calls without
 * expanding any; a file edit adds its ``+A −R`` line count. The expanded
 * body is shaped per tool (`toolBodies/ToolBody`): a diff, a terminal
 * block, a key/value list, or the raw JSON for a tool nothing knows.
 */
export function ToolCallPair({ use, result, forceOpen }: Props) {
  const sub = useRowSubagent(use);
  // An `Agent` call's own result is not when its work ends: a background agent's
  // comes back the moment it is launched. Its subagent's state is.
  const toolStatus = deriveToolStatus(use, result);
  const status =
    sub && toolStatus.kind !== "error"
      ? sub.summary.state === "running"
        ? ({ kind: "pending", label: "running…" } as const)
        : ({ kind: "success", label: "ok" } as const)
      : toolStatus;
  const name = toolDisplayName(use);
  const preview = toolPreview(use, result);
  const diff = toolDiffStats(use);

  const StatusIcon =
    status.kind === "success" ? Check : status.kind === "error" ? X : Loader2;
  const iconColor =
    status.kind === "success"
      ? "text-success"
      : status.kind === "error"
        ? "text-destructive"
        : "text-muted-foreground animate-spin";

  const input = (use.content as { input?: unknown } | undefined)?.input;
  const toolName = String(
    (use.content as { name?: unknown } | undefined)?.name ?? "tool",
  );

  return (
    <details
      data-turn-index={use.turn_index}
      // ``open`` controls the row when forceOpen is set; otherwise
      // ``open={undefined}`` lets the native toggle take over so single
      // rows still expand/collapse on click.
      open={forceOpen}
      className="group my-1 rounded border border-border bg-muted/40 text-sm"
    >
      <summary className="block cursor-pointer px-2 py-1.5 text-muted-foreground hover:bg-muted/60 select-none [&::-webkit-details-marker]:hidden">
        <div className="flex items-center gap-2">
          <ChevronRight className="h-3 w-3 shrink-0 transition-transform group-open:rotate-90" />
          <StatusIcon className={`h-3.5 w-3.5 shrink-0 ${iconColor}`} />
          <span className="font-mono text-xs font-medium text-foreground">
            {name}
          </span>
          {preview && (
            <span className="truncate font-mono text-xs text-muted-foreground">
              · {preview}
            </span>
          )}
          {diff && (diff.added > 0 || diff.removed > 0) && (
            <span
              data-testid="tool-diff-stats"
              className="shrink-0 whitespace-nowrap font-mono text-xs"
            >
              <span className="text-success">+{diff.added}</span>{" "}
              <span className="text-destructive">−{diff.removed}</span>
            </span>
          )}
          {sub && (
            <span className="ml-auto shrink-0 whitespace-nowrap text-xs text-muted-foreground">
              {sub.summary.state} · {subagentProgress(sub.summary)}
              {sub.nested > 0 &&
                ` · ${sub.nested} nested${sub.nestedRunning ? ` (${sub.nestedRunning} running)` : ""}`}
            </span>
          )}
        </div>
        {sub && sub.summary.state === "running" && sub.summary.last && (
          // The latest step, visible without opening the row: what "running"
          // means right now.
          <div className="truncate pl-[3.25rem] pt-0.5 font-mono text-xs text-muted-foreground">
            last: {sub.summary.last}
          </div>
        )}
      </summary>
      <div className="border-t border-border/60 p-2">
        <ToolBody
          name={toolName}
          input={input}
          result={result}
          isError={status.kind === "error"}
        />
      </div>
    </details>
  );
}
