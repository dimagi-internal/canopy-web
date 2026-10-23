import { createContext, useContext } from "react";

import type { Message, SubagentSummary } from "./protocol";

/** The session's subagents, for the `Agent` rows wherever they render (a
 *  grouped run, a lone row, an unpaired result). Provided by `ChatPanel` from
 *  `SessionState.subagents`. */
export const SubagentsContext = createContext<SubagentSummary[]>([]);

export interface AgentRowSubagent {
  summary: SubagentSummary;
  /** Subagents this one started, and how many of those are still running. */
  nested: number;
  nestedRunning: number;
}

/** The subagent an `Agent` tool row started, or null (not an `Agent` call, or
 *  the runner has not reported it). */
export function useRowSubagent(use: Message): AgentRowSubagent | null {
  const all = useContext(SubagentsContext);
  return subagentForRow(use, all);
}

export function subagentForRow(
  use: Message,
  all: SubagentSummary[],
): AgentRowSubagent | null {
  const content = use.content as { id?: unknown; name?: unknown } | undefined;
  if (content?.name !== "Agent" || typeof content.id !== "string" || !content.id) return null;
  const summary = all.find((s) => s.tool_use_id === content.id);
  if (!summary) return null;
  const children = descendants(summary.agent_id, all);
  return {
    summary,
    nested: children.length,
    nestedRunning: children.filter((s) => s.state === "running").length,
  };
}

function descendants(agentId: string, all: SubagentSummary[]): SubagentSummary[] {
  const out: SubagentSummary[] = [];
  const queue = [agentId];
  const seen = new Set(queue);
  while (queue.length) {
    const parent = queue.shift();
    for (const s of all) {
      if (s.parent_agent_id === parent && !seen.has(s.agent_id)) {
        seen.add(s.agent_id);
        out.push(s);
        queue.push(s.agent_id);
      }
    }
  }
  return out;
}

/** "37 steps · 4m" — how much it has done and over how long (first record to
 *  latest, so it needs no clock and says the same thing on every screen). */
export function subagentProgress(s: SubagentSummary): string {
  const parts = [`${s.steps} step${s.steps === 1 ? "" : "s"}`];
  const span = duration(s.started_at, s.updated_at);
  if (span) parts.push(span);
  return parts.join(" · ");
}

function duration(from: string | null, to: string | null): string {
  if (!from || !to) return "";
  const ms = Date.parse(to) - Date.parse(from);
  if (!Number.isFinite(ms) || ms < 0) return "";
  const min = Math.floor(ms / 60_000);
  if (min < 1) return `${Math.max(1, Math.round(ms / 1000))}s`;
  if (min < 60) return `${min}m`;
  return `${Math.floor(min / 60)}h ${min % 60}m`;
}
