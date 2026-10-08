import { apiV2 } from "./client.v2";
import { problemMessage } from "./problem";
import type { components } from "./generated";

/** A bounded, moderated conversation between named agents (apps/threads). */
export type AgentThread = components["schemas"]["ThreadOut"];
/** One message: a tagged turn for its speaker, with the speaker's parsed reply. */
export type ThreadMessage = components["schemas"]["ThreadMessageOut"];

/** Threads the caller can see, newest first, without their messages.
 * `parentKey`/`parentValue` keep those hanging off one thing (a huddle). */
export async function listThreads(
  opts: { parentKey?: string; parentValue?: string; agent?: string; status?: string } = {},
): Promise<AgentThread[]> {
  const { data, error } = await apiV2.GET("/api/threads/", {
    params: {
      query: { parent_key: opts.parentKey, parent_value: opts.parentValue, agent: opts.agent, status: opts.status },
    },
  });
  if (error) throw new Error(problemMessage(error, "Failed to load conversations"));
  return Array.from(data) as unknown as AgentThread[];
}

export async function getThread(id: string): Promise<AgentThread> {
  const { data, error } = await apiV2.GET("/api/threads/{thread_id}", {
    params: { path: { thread_id: id } },
  });
  if (error) throw new Error(problemMessage(error, "Failed to load the conversation"));
  return data as unknown as AgentThread;
}

/** The threads hanging off one huddle, each with its messages (the list carries
 * none). A huddle has at most a handful — one per "in, with changes" answer. */
export async function getHuddleThreads(huddleId: string): Promise<AgentThread[]> {
  const rows = await listThreads({ parentKey: "huddle", parentValue: huddleId });
  return Promise.all(rows.map((t) => getThread(t.id)));
}
