/** API client for workspace apps: /api/workspace-apps (members) and /api/public-apps (anyone). */
import { apiV2 } from "./client.v2";
import { apiUrl, getCsrfToken } from "./base";
import type { components } from "./generated";
import { unwrap } from "./problem";
import type { AppOp } from "../dynamicViews/protocol";

export type StoreApp = components["schemas"]["StoreAppOut"];
export type StoreAppDetail = components["schemas"]["StoreAppDetailOut"];
export type InstalledAppSummary = components["schemas"]["InstallationSummaryOut"];
export type InstalledApp = components["schemas"]["InstallationOut"];
export type PublicApp = components["schemas"]["PublicAppOut"];
export type AppPage = components["schemas"]["PageOut"];
export type AppVersionRow = components["schemas"]["VersionOut"];
export type AppGrant = components["schemas"]["GrantOut"];

export interface ReviewChange {
  id: string;
  op: string;
  summary: string;
  intent: string;
  by: string;
  at: string;
  new: boolean;
  risk: string | null;
}
export interface Review {
  changes: ReviewChange[];
  risks: ReviewChange[];
  quarantine: { id: string; summary: string; intent: string; reason: string }[];
  template_change: { from: string; to: string; changelog: string } | null;
  forked_pages: string[];
  checks: { ok?: boolean; ran_at?: string; quarantined?: number; results?: { name: string; ok: boolean; error?: string; origin: string }[] };
}
export type AppDraft = Omit<components["schemas"]["DraftOut"], "review"> & { review: Review };
/** A page source naming one of the app's own collections (the rest are dynamic-view catalog reads). */
export type CollectionSource = { alias: string; collection: string; where?: Record<string, unknown>; order?: string; limit?: number };

const P = (workspace: string, key: string) => ({ workspace, key });

export async function listAppStore(workspace: string): Promise<StoreApp[]> {
  const res = await apiV2.GET("/api/workspace-app-store/{workspace}/", { params: { path: { workspace } } });
  return Array.from(unwrap(res, "Failed to load the app store"));
}

export async function getAppStoreDetail(workspace: string, template: string): Promise<StoreAppDetail> {
  const res = await apiV2.GET("/api/workspace-app-store/{workspace}/detail", {
    params: { path: { workspace }, query: { template } },
  });
  return unwrap(res, "Failed to load the app") as unknown as StoreAppDetail;
}

/** A store app's screenshot, as a URL an <img> can load (same-origin, the session's cookie). */
export function appStoreScreenshotUrl(workspace: string, template: string, file: string): string {
  const q = new URLSearchParams({ template, file });
  return apiUrl(`/api/workspace-app-store/${encodeURIComponent(workspace)}/screenshot?${q}`);
}

export async function listInstalledApps(workspace: string): Promise<InstalledAppSummary[]> {
  const res = await apiV2.GET("/api/workspace-apps/{workspace}/", { params: { path: { workspace } } });
  return Array.from(unwrap(res, "Failed to load the workspace's apps"));
}

export async function getInstalledApp(workspace: string, key: string, draft?: number | null): Promise<InstalledApp> {
  const res = await apiV2.GET("/api/workspace-apps/{workspace}/{key}", {
    params: { path: P(workspace, key), query: { draft: draft ?? undefined } },
  });
  return unwrap(res, "Failed to load the app") as unknown as InstalledApp;
}

export async function installApp(workspace: string, template: string): Promise<InstalledApp> {
  const res = await apiV2.POST("/api/workspace-apps/{workspace}/", {
    params: { path: { workspace } },
    body: { template, manifest: null },
  });
  return unwrap(res, "Failed to install the app") as unknown as InstalledApp;
}

export async function updateInstalledApp(workspace: string, key: string, patch: { enabled?: boolean; public?: boolean }): Promise<InstalledApp> {
  const res = await apiV2.PATCH("/api/workspace-apps/{workspace}/{key}", {
    params: { path: P(workspace, key) },
    body: { enabled: patch.enabled ?? null, public: patch.public ?? null },
  });
  return unwrap(res, "Failed to change the app") as unknown as InstalledApp;
}

export async function uninstallApp(workspace: string, key: string): Promise<void> {
  const res = await apiV2.DELETE("/api/workspace-apps/{workspace}/{key}", { params: { path: P(workspace, key) } });
  if (res.error) unwrap(res, "Failed to remove the app");
}

// --- the life cycle --------------------------------------------------------------------------

export async function getDraft(workspace: string, key: string): Promise<AppDraft | null> {
  const res = await apiV2.GET("/api/workspace-apps/{workspace}/{key}/draft", { params: { path: P(workspace, key) } });
  if (res.response.status === 404) return null;
  return unwrap(res, "Failed to load the draft") as unknown as AppDraft;
}

export async function startDraft(workspace: string, key: string, opts: { upgrade?: boolean; note?: string } = {}): Promise<AppDraft> {
  const res = await apiV2.POST("/api/workspace-apps/{workspace}/{key}/draft", {
    params: { path: P(workspace, key) },
    body: { upgrade: opts.upgrade ?? false, note: opts.note ?? "" },
  });
  return unwrap(res, "Failed to start a draft") as unknown as AppDraft;
}

export async function discardDraft(workspace: string, key: string): Promise<void> {
  const res = await apiV2.DELETE("/api/workspace-apps/{workspace}/{key}/draft", { params: { path: P(workspace, key) } });
  if (res.error) unwrap(res, "Failed to discard the draft");
}

export async function addChange(workspace: string, key: string, op: Record<string, unknown>): Promise<AppDraft> {
  const res = await apiV2.POST("/api/workspace-apps/{workspace}/{key}/draft/ops", {
    params: { path: P(workspace, key) },
    body: op as never,
  });
  return unwrap(res, "That change could not be made") as unknown as AppDraft;
}

export async function removeChange(workspace: string, key: string, opId: string): Promise<AppDraft> {
  const res = await apiV2.DELETE("/api/workspace-apps/{workspace}/{key}/draft/ops/{op_id}", {
    params: { path: { ...P(workspace, key), op_id: opId } },
  });
  return unwrap(res, "Failed to remove the change") as unknown as AppDraft;
}

export async function runChecks(workspace: string, key: string): Promise<AppDraft> {
  const res = await apiV2.POST("/api/workspace-apps/{workspace}/{key}/draft/checks", { params: { path: P(workspace, key) } });
  return unwrap(res, "Failed to run the checks") as unknown as AppDraft;
}

export async function publishDraft(workspace: string, key: string): Promise<InstalledApp> {
  const res = await apiV2.POST("/api/workspace-apps/{workspace}/{key}/draft/publish", { params: { path: P(workspace, key) } });
  return unwrap(res, "Could not publish") as unknown as InstalledApp;
}

export async function listVersions(workspace: string, key: string): Promise<AppVersionRow[]> {
  const res = await apiV2.GET("/api/workspace-apps/{workspace}/{key}/versions", { params: { path: P(workspace, key) } });
  return Array.from(unwrap(res, "Failed to load the versions"));
}

export async function rollbackTo(workspace: string, key: string, number: number): Promise<InstalledApp> {
  const res = await apiV2.POST("/api/workspace-apps/{workspace}/{key}/rollback", {
    params: { path: P(workspace, key) },
    body: { number },
  });
  return unwrap(res, "Could not roll back") as unknown as InstalledApp;
}

export async function listGrants(workspace: string, key: string): Promise<AppGrant[]> {
  const res = await apiV2.GET("/api/workspace-apps/{workspace}/{key}/grants", { params: { path: P(workspace, key) } });
  return Array.from(unwrap(res, "Failed to load who has which role"));
}

export async function addGrant(workspace: string, key: string, email: string, role: string): Promise<AppGrant> {
  const res = await apiV2.POST("/api/workspace-apps/{workspace}/{key}/grants", {
    params: { path: P(workspace, key) },
    body: { email, role },
  });
  return unwrap(res, "Could not give the role");
}

export async function removeGrant(workspace: string, key: string, grantId: number): Promise<void> {
  const res = await apiV2.DELETE("/api/workspace-apps/{workspace}/{key}/grants/{grant_id}", {
    params: { path: { ...P(workspace, key), grant_id: grantId } },
  });
  if (res.error) unwrap(res, "Could not take the role back");
}

// --- answering an app page's calls --------------------------------------------------------------

function whereParam(where?: Record<string, unknown>): string {
  return where && Object.keys(where).length ? JSON.stringify(where) : "";
}

async function postFile(url: string, op: Extract<AppOp, { op: "upload" }>): Promise<unknown> {
  const form = new FormData();
  form.append("collection", op.collection);
  form.append("field", op.field);
  form.append("file", new Blob([op.bytes], { type: op.type || "application/octet-stream" }), op.name);
  const res = await fetch(apiUrl(url), {
    method: "POST", body: form, credentials: "same-origin", headers: { "X-CSRFToken": getCsrfToken() },
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new Error((body && (body.detail || body.title)) || `upload failed (${res.status})`);
  return body;
}

async function openFile(url: string): Promise<null> {
  const res = await fetch(apiUrl(url), { credentials: "same-origin" });
  if (!res.ok) throw new Error(`could not open the file (${res.status})`);
  const blob = await res.blob();
  window.open(URL.createObjectURL(blob), "_blank", "noopener");
  return null;
}

/**
 * Answer an app page's call. `door`:
 * - `member`: as the viewer, on the live app or (with `draft`) a draft's test copy;
 * - `preview`: a public page previewed by a maintainer on a draft's test copy,
 *   with the visitor's rules (`as_public`);
 * - `public`: an anonymous visitor on the live public door.
 */
async function postJson(url: string, body: unknown, failure: string): Promise<unknown> {
  const res = await fetch(apiUrl(url), {
    method: "POST", body: JSON.stringify(body), credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
  });
  const out = await res.json().catch(() => null);
  if (!res.ok) throw new Error((out && (out.detail || out.title)) || `${failure} (${res.status})`);
  return out;
}

/**
 * Answer a LINK page's call: the holder of a private link may only run that
 * link's actions, on its one record; there is nothing else to read.
 */
async function callLink(workspace: string, key: string, token: string, op: AppOp): Promise<unknown> {
  if (op.op !== "act") throw new Error("not available on this page");
  const url = `/api/public-apps/${encodeURIComponent(workspace)}/${encodeURIComponent(key)}/link/${encodeURIComponent(token)}/actions/${encodeURIComponent(op.action)}`;
  return postJson(url, { input: op.input ?? {} }, "That did not work");
}

export async function callApp(workspace: string, key: string, op: AppOp,
                              door: "member" | "preview" | "public" | "link", draft?: number | null,
                              token?: string): Promise<unknown> {
  if (door === "link") return callLink(workspace, key, token ?? "", op);
  const path = P(workspace, key);
  const isPublic = door === "public";
  const asPublic = door === "preview" ? true : undefined;
  const d = draft ?? undefined;
  const base = isPublic ? `/api/public-apps/${encodeURIComponent(workspace)}/${encodeURIComponent(key)}`
    : `/api/workspace-apps/${encodeURIComponent(workspace)}/${encodeURIComponent(key)}`;
  if (op.op === "query") {
    const query = { collection: op.collection, where: whereParam(op.where), order: op.order ?? "-created_at", limit: op.limit ?? 100 };
    const res = isPublic
      ? await apiV2.GET("/api/public-apps/{workspace}/{key}/query", { params: { path, query } })
      : await apiV2.GET("/api/workspace-apps/{workspace}/{key}/query", { params: { path, query: { ...query, draft: d, as_public: asPublic } } });
    return unwrap(res, "The read failed");
  }
  if (op.op === "act") {
    const body = { record: op.record ?? null, input: op.input ?? {}, params: op.params ?? {} };
    const res = isPublic
      ? await apiV2.POST("/api/public-apps/{workspace}/{key}/actions/{action}", { params: { path: { ...path, action: op.action } }, body })
      : await apiV2.POST("/api/workspace-apps/{workspace}/{key}/actions/{action}", {
        params: { path: { ...path, action: op.action }, query: { draft: d, as_public: asPublic } }, body,
      });
    return unwrap(res, "The action failed");
  }
  const q = d ? `?draft=${d}` : "";
  if (op.op === "upload") return postFile(`${base}/files${isPublic ? "" : q}`, op);
  if (isPublic || door === "preview") throw new Error("not available on a public page");
  if (op.op === "bulk") {
    return postJson(`${base}/actions/${encodeURIComponent(op.action)}/bulk${q}`,
      { records: op.records, input: op.input ?? {}, params: op.params ?? {} }, "The bulk action failed");
  }
  if (op.op === "link") {
    return postJson(`${base}/links/${encodeURIComponent(op.link)}/${op.record}${q}`, {}, "Could not make the link");
  }
  if (op.op === "open_file") return openFile(`${base}/files/${op.id}${q}`);
  const res = await apiV2.GET("/api/workspace-apps/{workspace}/{key}/records/{record_id}/history", {
    params: { path: { ...path, record_id: op.record }, query: { draft: d } },
  });
  return unwrap(res, "Failed to load the history");
}

export async function getPublicApp(workspace: string, key: string): Promise<PublicApp> {
  const res = await apiV2.GET("/api/public-apps/{workspace}/{key}", { params: { path: P(workspace, key) } });
  return unwrap(res, "This page is not available") as unknown as PublicApp;
}

export interface LinkPage {
  key: string; name: string; workspace: string; workspace_name: string; link: string; label: string;
  record: Record<string, unknown>; page: AppPage; actions: Record<string, unknown>;
  data_model: Record<string, unknown>; settings: Record<string, unknown>; draft: boolean;
}

export async function getLinkPage(workspace: string, key: string, token: string): Promise<LinkPage> {
  const res = await fetch(apiUrl(`/api/public-apps/${encodeURIComponent(workspace)}/${encodeURIComponent(key)}/link/${encodeURIComponent(token)}`),
    { credentials: "same-origin" });
  if (!res.ok) throw new Error("This link is not valid");
  return res.json();
}

/** A public collection's JobPosting entries (Google for Jobs structured data). */
export async function getPublicFeed(workspace: string, key: string, collection: string): Promise<Record<string, unknown>[]> {
  const res = await fetch(apiUrl(`/api/public-apps/${encodeURIComponent(workspace)}/${encodeURIComponent(key)}/feed/${encodeURIComponent(collection)}.json`));
  return res.ok ? res.json() : [];
}

export interface RetentionRule {
  name: string; label: string; collection: string; does: "anonymize" | "delete"; count: number;
  examples: { id: number; title: unknown }[];
}

async function getJson(url: string, failure: string): Promise<unknown> {
  const res = await fetch(apiUrl(url), { credentials: "same-origin" });
  const out = await res.json().catch(() => null);
  if (!res.ok) throw new Error((out && (out.detail || out.title)) || `${failure} (${res.status})`);
  return out;
}

const appBase = (workspace: string, key: string) =>
  `/api/workspace-apps/${encodeURIComponent(workspace)}/${encodeURIComponent(key)}`;

export async function retentionPreview(workspace: string, key: string): Promise<RetentionRule[]> {
  return getJson(`${appBase(workspace, key)}/retention`, "Could not read the retention rules") as Promise<RetentionRule[]>;
}

export async function retentionApply(workspace: string, key: string): Promise<Record<string, number>> {
  return postJson(`${appBase(workspace, key)}/retention`, {}, "Could not run the rules") as Promise<Record<string, number>>;
}

export async function subjectExport(workspace: string, key: string, email: string): Promise<unknown> {
  return getJson(`${appBase(workspace, key)}/subject-export?email=${encodeURIComponent(email)}`, "Could not export");
}
