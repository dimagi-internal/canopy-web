import { apiV2 } from "./client.v2";
import { problemMessage } from "./problem";
import type { components } from "./generated";

export type LogEvent = components["schemas"]["EventOut"];
export type McpCall = components["schemas"]["McpCallOut"];

/** The workspace event log — admins and owners only (`logs.read`); anyone
 * else gets an empty list, not an error. Scope comes from the URL like every
 * other tenant read. */
export async function listEvents(limit = 100): Promise<LogEvent[]> {
  const { data, error } = await apiV2.GET("/api/events/", { params: { query: { limit } } });
  if (error) throw new Error(problemMessage(error, "Failed to load the event log"));
  return Array.from(data.items);
}

/** MCP tool calls: the workspace's for admins and owners, your own for anyone. */
export async function listMcpCalls(limit = 100, failed = false): Promise<McpCall[]> {
  const { data, error } = await apiV2.GET("/api/events/mcp-calls", {
    params: { query: { limit, failed } },
  });
  if (error) throw new Error(problemMessage(error, "Failed to load MCP calls"));
  return Array.from(data.items);
}
