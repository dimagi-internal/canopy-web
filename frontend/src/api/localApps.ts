/** API client for local apps, programs from Canopy's store that run on a person's own computer: /api/local-apps. */
import { apiV2 } from "./client.v2";
import type { components } from "./generated";
import { ensureOk, unwrap } from "./problem";

export type LocalAppStore = components["schemas"]["LocalAppStoreOut"];
export type LocalAppListing = components["schemas"]["LocalAppListingOut"];
export type LocalAppDetail = components["schemas"]["LocalAppDetailOut"];
export type LocalAppInstallation = components["schemas"]["LocalAppInstallationOut"];
export type LocalAppRun = components["schemas"]["LocalAppRunOut"];
export type LocalAppReview = components["schemas"]["LocalAppReviewOut"];
export type LocalAppSubmission = components["schemas"]["LocalAppSubmissionOut"];

/** One entry of a manifest's `settings`; the schema types these as open objects. */
export interface LocalAppSetting {
  type: "agent" | "text" | "longtext" | "bool";
  label: string;
  help: string;
  required: boolean;
  default?: unknown;
}
export type LocalAppSettingsSpec = Record<string, LocalAppSetting>;
export type LocalAppSettingValue = string | boolean;

/** `facts` on a submission: what a reviewer should look at first. */
export interface LocalAppFacts {
  files: { path: string; size: number }[];
  network: Record<string, string[]>;
  process: Record<string, string[]>;
  risky: Record<string, string[]>;
  unparsed: string[];
}

/** One file of a submission's `diff`: unified diff lines, cut off at the server's limit. */
export interface LocalAppFileDiff {
  path: string;
  status: "added" | "removed" | "changed";
  lines: string[];
  truncated: boolean;
}

export function settingsSpecOf(raw: LocalAppDetail["settings"] | LocalAppInstallation["settings_spec"]): LocalAppSettingsSpec {
  // Sorted by `order` because the database doesn't keep a JSON object's key order.
  const entries = Object.entries(raw as unknown as Record<string, LocalAppSettingsSpec[string] & { order?: number }>);
  entries.sort(([, a], [, b]) => (a.order ?? 0) - (b.order ?? 0));
  return Object.fromEntries(entries) as LocalAppSettingsSpec;
}

export function factsOf(review: LocalAppReview): LocalAppFacts {
  const f = review.facts as unknown as Partial<LocalAppFacts>;
  return { files: f.files ?? [], network: f.network ?? {}, process: f.process ?? {}, risky: f.risky ?? {}, unparsed: f.unparsed ?? [] };
}

export function diffOf(review: LocalAppReview): LocalAppFileDiff[] {
  return review.diff as unknown as LocalAppFileDiff[];
}

export async function getLocalAppStore(workspace: string): Promise<LocalAppStore> {
  const res = await apiV2.GET("/api/local-apps/store/{workspace}/", { params: { path: { workspace } } });
  return unwrap(res, "Failed to load apps that run on your computer") as unknown as LocalAppStore;
}

export async function setLocalAppsSwitch(workspace: string, enabled: boolean): Promise<void> {
  const res = await apiV2.PUT("/api/local-apps/store/{workspace}/switch", {
    params: { path: { workspace } },
    body: { enabled },
  });
  ensureOk(res, "Failed to change the setting");
}

export async function getLocalApp(workspace: string, key: string): Promise<LocalAppDetail> {
  const res = await apiV2.GET("/api/local-apps/store/{workspace}/apps/{key}", { params: { path: { workspace, key } } });
  return unwrap(res, "Failed to load the app") as unknown as LocalAppDetail;
}

export async function installLocalApp(
  workspace: string,
  key: string,
  install: { runner_id: string; settings: Record<string, LocalAppSettingValue>; accept_digest: string },
): Promise<LocalAppInstallation> {
  const res = await apiV2.POST("/api/local-apps/store/{workspace}/apps/{key}/install", {
    params: { path: { workspace, key } },
    body: install,
  });
  return unwrap(res, "Failed to install the app") as unknown as LocalAppInstallation;
}

/** This person's installations, on all their computers and across workspaces. */
export async function listMyLocalApps(): Promise<LocalAppInstallation[]> {
  const res = await apiV2.GET("/api/local-apps/installations/");
  return Array.from(unwrap(res, "Failed to load your installed apps")) as LocalAppInstallation[];
}

export async function configureLocalApp(id: number, settings: Record<string, LocalAppSettingValue>): Promise<LocalAppInstallation> {
  const res = await apiV2.PATCH("/api/local-apps/installations/{installation_id}", {
    params: { path: { installation_id: id } },
    body: { settings },
  });
  return unwrap(res, "Failed to save the settings") as unknown as LocalAppInstallation;
}

export async function acceptLocalAppChanges(id: number, digest: string): Promise<LocalAppInstallation> {
  const res = await apiV2.POST("/api/local-apps/installations/{installation_id}/accept", {
    params: { path: { installation_id: id } },
    body: { digest },
  });
  return unwrap(res, "Failed to accept the changes") as unknown as LocalAppInstallation;
}

export async function removeLocalApp(id: number): Promise<void> {
  const res = await apiV2.DELETE("/api/local-apps/installations/{installation_id}", {
    params: { path: { installation_id: id } },
  });
  ensureOk(res, "Failed to remove the app");
}

export async function stopLocalAppRun(runId: string): Promise<LocalAppRun> {
  const res = await apiV2.POST("/api/local-apps/runs/{run_id}/stop", { params: { path: { run_id: runId } } });
  return unwrap(res, "Failed to stop the app") as unknown as LocalAppRun;
}

export async function getLocalAppSubmission(id: number): Promise<LocalAppReview> {
  const res = await apiV2.GET("/api/local-apps/submissions/{submission_id}", { params: { path: { submission_id: id } } });
  return unwrap(res, "Failed to load the submission") as unknown as LocalAppReview;
}

export async function revokeLocalAppVersion(id: number, reason: string): Promise<LocalAppSubmission> {
  const res = await apiV2.POST("/api/local-apps/submissions/{submission_id}/revoke", {
    params: { path: { submission_id: id } },
    body: { reason },
  });
  return unwrap(res, "Failed to revoke the version") as unknown as LocalAppSubmission;
}
