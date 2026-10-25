import { Loader2 } from "lucide-react";

import type { SubagentSummary } from "./protocol";
import { subagentProgress } from "./subagents";

/** The session's top-level subagents still running, pinned above the composer.
 *
 *  Each one's `Agent` row sits where it was started, which a busy turn scrolls
 *  out of view within minutes while the agent works on, so nothing at the
 *  bottom said anything was still going. Nested agents are counted on their
 *  parent's row, not listed here. Clicking a line scrolls to that row. */
export function RunningAgents({
  subagents, onShow,
}: {
  subagents: SubagentSummary[];
  onShow?: (toolUseId: string) => void;
}) {
  const running = subagents.filter((s) => s.state === "running" && !s.parent_agent_id);
  if (running.length === 0) return null;
  return (
    <div data-testid="running-agents"
         className="max-h-32 overflow-y-auto border-t border-border bg-background px-3 py-1">
      {running.map((s) => (
        <button key={s.agent_id} type="button" data-testid="running-agent"
                onClick={() => onShow?.(s.tool_use_id)}
                title="Show where it was started"
                className="flex w-full items-center gap-2 rounded px-1 py-0.5 text-left text-xs text-muted-foreground hover:bg-muted">
          <Loader2 className="h-3 w-3 shrink-0 animate-spin" aria-hidden="true" />
          <span className="shrink-0 font-medium text-foreground">{s.description || s.agent_type || "Agent"}</span>
          {s.last && <span className="min-w-0 truncate font-mono">· {s.last}</span>}
          <span className="ml-auto shrink-0 whitespace-nowrap">{subagentProgress(s)}</span>
        </button>
      ))}
    </div>
  );
}
