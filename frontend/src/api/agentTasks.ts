/**
 * API client for /api/agent-tasks — give an agent a task and get a structured result
 * (docs/architecture/agent-tasks.md). Every call names its workspace, so it goes to
 * `/api/w/<ws>/agent-tasks/…` whatever page it is made from.
 */
import { WORKSPACE_HEADER, apiV2 } from "./client.v2";
import type { components } from "./generated";
import { unwrap } from "./problem";

export type AgentTask = components["schemas"]["AgentTaskCallOut"];
export type AgentTaskIn = components["schemas"]["AgentTaskCallIn"];

const pinned = (workspace: string) => ({ [WORKSPACE_HEADER]: workspace });

export async function createAgentTask(workspace: string, body: AgentTaskIn): Promise<AgentTask> {
  const res = await apiV2.POST("/api/agent-tasks/", { body, headers: pinned(workspace) });
  return unwrap(res, "Failed to start the agent task") as AgentTask;
}

/** The task; `waitS` (0–20) long-polls until its status changes. */
export async function getAgentTask(workspace: string, id: string, waitS = 0, signal?: AbortSignal): Promise<AgentTask> {
  const res = await apiV2.GET("/api/agent-tasks/{task_id}", {
    params: { path: { task_id: id }, query: { wait_s: waitS } },
    headers: pinned(workspace),
    signal,
  });
  return unwrap(res, "Failed to read the agent task") as AgentTask;
}

export async function cancelAgentTask(workspace: string, id: string): Promise<AgentTask> {
  const res = await apiV2.POST("/api/agent-tasks/{task_id}/cancel", {
    params: { path: { task_id: id } },
    headers: pinned(workspace),
  });
  return unwrap(res, "Failed to cancel the agent task") as AgentTask;
}
