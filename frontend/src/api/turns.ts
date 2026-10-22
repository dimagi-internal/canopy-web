import { apiV2 } from "./client.v2";
import { unwrap } from "./problem";
import type { components } from "./generated";
import type { Turn } from "@/components/activity/turnLog";

export type TurnEvent = components["schemas"]["TurnEventOut"];

/** Last N turns across the current scope (all my workspaces on /activity, one
 * tenant on /w/:ws/activity — the api client picks scope from the URL). */
export async function listTurns(limit = 20): Promise<Turn[]> {
  const res = await apiV2.GET("/api/harness/turns/", {
    params: { query: { limit } },
  });
  const data = unwrap(res, "Failed to load activity");
  // Newest first, and only `limit` of them. The server already orders it that
  // way, but a caller in several organisations is answered one workspace at a
  // time on a root page (../api/workspaceRetry), and that merge concatenates.
  return [...(data as Turn[])]
    .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))
    .slice(0, limit);
}

/** The append-only event ledger for one turn (drill-down). */
export async function listTurnEvents(turnId: string): Promise<TurnEvent[]> {
  const res = await apiV2.GET("/api/harness/turns/{turn_id}/events", {
    params: { path: { turn_id: turnId } },
  });
  const data = unwrap(res, "Failed to load turn events");
  return data.events as TurnEvent[];
}

export type TurnMessages = components["schemas"]["TurnMessagesOut"];

/** A turn's retained transcript as readable messages — how you see what a
 * cloud-runner turn did, since it has no chat. Bounded server-side; `truncated`
 * says the view stopped early. */
export async function listTurnMessages(turnId: string): Promise<TurnMessages> {
  const res = await apiV2.GET("/api/harness/turns/{turn_id}/messages", {
    params: { path: { turn_id: turnId } },
  });
  const data = unwrap(res, "Failed to load the transcript");
  return { ...data, messages: Array.from(data.messages) };
}
