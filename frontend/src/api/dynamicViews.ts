/** API client for /api/dynamic-views — AI-generated screens drawn in a sandbox. */
import { apiV2 } from "./client.v2";
import type { components } from "./generated";
import { unwrap } from "./problem";
import type { CatalogModel } from "../dynamicViews/bridge";

export type DynamicViewSummary = components["schemas"]["DynamicViewSummaryOut"];
export type DynamicView = components["schemas"]["DynamicViewOut"];
export type DynamicViewRevision = components["schemas"]["DynamicViewRevisionOut"];
export type DynamicViewGuide = Omit<components["schemas"]["DynamicViewGuideOut"], "catalog"> & { catalog: CatalogModel[] };
export type DynamicViewSource = { alias: string; model: string; params?: Record<string, unknown>; path?: Record<string, unknown> };

export async function listDynamicViews(project?: string): Promise<DynamicViewSummary[]> {
  const res = await apiV2.GET("/api/dynamic-views/", { params: { query: { project: project || undefined } } });
  return Array.from(unwrap(res, "Failed to load views")) as unknown as DynamicViewSummary[];
}

export async function getDynamicView(id: number): Promise<DynamicView> {
  const res = await apiV2.GET("/api/dynamic-views/{view_id}", { params: { path: { view_id: id } } });
  return unwrap(res, "Failed to load the view") as unknown as DynamicView;
}

let guide: Promise<DynamicViewGuide> | null = null;

/** The catalog and contract; the same for every view, so read once per page load. */
export function getDynamicViewGuide(): Promise<DynamicViewGuide> {
  guide ??= apiV2.GET("/api/dynamic-views/guide/", {})
    .then((res) => unwrap(res, "Failed to load the view catalog") as unknown as DynamicViewGuide)
    .catch((e) => {
      guide = null;
      throw e;
    });
  return guide;
}

export async function createDynamicView(body: {
  workspace: string;
  project?: string | null;
  title: string;
  description?: string;
  code: string;
  sources: DynamicViewSource[];
  agent_tasks?: { agent: string; name: string }[];
}): Promise<DynamicView> {
  const res = await apiV2.POST("/api/dynamic-views/", {
    body: {
      ...body, project: body.project || null, description: body.description ?? "", agent_tasks: body.agent_tasks ?? [],
      slug: "", note: "",
    },
  });
  return unwrap(res, "Failed to create the view") as unknown as DynamicView;
}

export async function updateDynamicView(
  id: number,
  patch: {
    title?: string; description?: string; code?: string; sources?: DynamicViewSource[];
    agent_tasks?: { agent: string; name: string }[]; note?: string;
  },
): Promise<DynamicView> {
  const res = await apiV2.PATCH("/api/dynamic-views/{view_id}", {
    params: { path: { view_id: id } },
    body: { note: "", ...patch },
  });
  return unwrap(res, "Failed to save the view") as unknown as DynamicView;
}

export async function deleteDynamicView(id: number): Promise<void> {
  const res = await apiV2.DELETE("/api/dynamic-views/{view_id}", { params: { path: { view_id: id } } });
  if (res.error) unwrap(res, "Failed to delete the view");
}

export async function listDynamicViewRevisions(id: number): Promise<DynamicViewRevision[]> {
  const res = await apiV2.GET("/api/dynamic-views/{view_id}/revisions/", { params: { path: { view_id: id } } });
  return Array.from(unwrap(res, "Failed to load the history")) as unknown as DynamicViewRevision[];
}

export async function rollbackDynamicView(id: number, version: number): Promise<DynamicView> {
  const res = await apiV2.POST("/api/dynamic-views/{view_id}/rollback", {
    params: { path: { view_id: id } },
    body: { version },
  });
  return unwrap(res, "Failed to restore that version") as unknown as DynamicView;
}
