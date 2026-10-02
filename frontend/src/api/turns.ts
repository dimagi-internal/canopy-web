import { apiV2 } from "./client.v2";
import { problemMessage } from "./problem";
import type { components } from "./generated";
import type { Turn } from "@/components/activity/turnLog";

export type TurnEvent = components["schemas"]["TurnEventOut"];

/** Last N turns across the current scope (all my workspaces on /activity, one
 * tenant on /w/:ws/activity — the api client picks scope from the URL). */
export async function listTurns(limit = 20): Promise<Turn[]> {
  const { data, error } = await apiV2.GET("/api/harness/turns/", {
    params: { query: { limit } },
  });
  if (error) throw new Error(problemMessage(error, "Failed to load activity"));
  return data as Turn[];
}

/** The append-only event ledger for one turn (drill-down). */
export async function listTurnEvents(turnId: string): Promise<TurnEvent[]> {
  const { data, error } = await apiV2.GET("/api/harness/turns/{turn_id}/events", {
    params: { path: { turn_id: turnId } },
  });
  if (error) throw new Error(problemMessage(error, "Failed to load turn events"));
  return data.events as TurnEvent[];
}

export type TurnMessages = components["schemas"]["TurnMessagesOut"];

/** A turn's retained transcript as readable messages — how you see what a
 * cloud-runner turn did, since it has no chat. Bounded server-side; `truncated`
 * says the view stopped early. */
export async function listTurnMessages(turnId: string): Promise<TurnMessages> {
  const { data, error } = await apiV2.GET("/api/harness/turns/{turn_id}/messages", {
    params: { path: { turn_id: turnId } },
  });
  if (error) throw new Error(problemMessage(error, "Failed to load the transcript"));
  return { ...data, messages: Array.from(data.messages) };
}
