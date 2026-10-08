import { apiV2 } from "./client.v2";
import { problemMessage } from "./problem";
import type { components } from "./generated";
import type { AgentThread } from "./threads";

/** A huddle as the list shows it — derived server-side from its anchor turn. */
export type HuddleSummary = components["schemas"]["HuddleSummaryOut"];
/** One huddle in full: a cell per (member, round), plus the tasks it produced —
 * and, joined on the page, the agent threads hanging off it (an agreement thread
 * per "in, with changes" answer, where the idea's lead and that teammate settle
 * the changes directly). `threads` carry their messages. */
export type Huddle = components["schemas"]["HuddleOut"] & { threads?: AgentThread[] };
export type HuddleCell = components["schemas"]["HuddleCellOut"];
export type HuddleOutput = components["schemas"]["HuddleOutputOut"];

/** Huddles the caller can see, newest first. `agent` keeps those it led or joined.
 * Scope (one workspace vs all of mine) comes from the URL, like every list. */
export async function listHuddles(opts: { agent?: string; limit?: number } = {}): Promise<HuddleSummary[]> {
  const { data, error } = await apiV2.GET("/api/huddles/", {
    params: { query: { agent: opts.agent, limit: opts.limit } },
  });
  if (error) throw new Error(problemMessage(error, "Failed to load huddles"));
  // openapi-fetch's Readable<T> degrades readonly arrays; the shape is the schema's.
  return Array.from(data) as unknown as HuddleSummary[];
}

export async function getHuddle(id: string): Promise<Huddle> {
  const { data, error } = await apiV2.GET("/api/huddles/{huddle_id}", {
    params: { path: { huddle_id: id } },
  });
  if (error) throw new Error(problemMessage(error, "Failed to load the huddle"));
  return data as unknown as Huddle;
}
