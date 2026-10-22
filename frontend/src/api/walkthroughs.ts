import { scopedPath } from "../lib/scopedLinks";
import { apiV2 } from "./client.v2";
import type { components } from "./generated";
import { withBase } from "../lib/basePath";
import { ApiError, ensureOk, unwrap } from "./problem";

/**
 * A failed walkthrough call, carrying the HTTP status — the shared `ApiError`
 * (./problem) under its old name. The status is the whole point: a private
 * walkthrough 404s to an anonymous caller (deliberate existence-hiding), and the
 * viewer needs the number to offer a sign-in instead of a dead end
 * (canopy-web#516).
 */
export { ApiError as WalkthroughApiError };

export type WalkthroughKind = "html" | "video";
export type WalkthroughVisibility = "private" | "link";

export type WalkthroughListItem = components["schemas"]["WalkthroughListItemOut"];
export type WalkthroughDetail = components["schemas"]["WalkthroughDetailOut"];
export type WalkthroughLink = components["schemas"]["WalkthroughLink"];
export type WalkthroughLinkKind = "narrative" | "companion" | "reference";

export interface WalkthroughListFilters {
  project?: string;
  kind?: WalkthroughKind;
  mine?: boolean;
}

export interface PatchWalkthroughInput {
  title?: string;
  description?: string;
  project_slug?: string | null;
  visibility?: WalkthroughVisibility;
}

export async function listWalkthroughs(
  filters: WalkthroughListFilters = {},
): Promise<WalkthroughListItem[]> {
  const res = await apiV2.GET("/api/walkthroughs/", {
    params: {
      query: {
        ...(filters.project ? { project: filters.project } : {}),
        ...(filters.kind ? { kind: filters.kind } : {}),
        ...(filters.mine ? { mine: "true" } : {}),
      },
    },
  });
  const data = unwrap(res, "Failed to load walkthroughs");
  return data as unknown as WalkthroughListItem[];
}

export async function getWalkthrough(
  id: string,
  token?: string | null,
  ws?: string | null,
): Promise<WalkthroughDetail> {
  const query = {
    ...(token ? { t: token } : {}),
    // The workspace the page's URL names — a walkthrough from another one 404s.
    ...(ws ? { ws } : {}),
  };
  const res = await apiV2.GET("/api/walkthroughs/{wid}/", {
    params: {
      path: { wid: id },
      ...(Object.keys(query).length ? { query } : {}),
    },
  });
  ensureOk(res, "Failed to load walkthrough");
  // openapi-fetch's immutable response type deep-freezes the `links` array
  // (method signatures become `{}`), which no longer structurally matches the
  // plain schema alias. Same cast `listWalkthroughs` uses for its array body.
  return res.data as unknown as WalkthroughDetail;
}

export async function patchWalkthrough(
  id: string,
  patch: PatchWalkthroughInput,
): Promise<WalkthroughDetail> {
  const res = await apiV2.PATCH("/api/walkthroughs/{wid}/", {
    params: { path: { wid: id } },
    body: {
      title: patch.title ?? null,
      description: patch.description ?? null,
      project_slug: patch.project_slug ?? null,
      visibility: patch.visibility ?? null,
    },
  });
  const data = unwrap(res, "Failed to update walkthrough");
  return data as unknown as WalkthroughDetail;
}

export async function deleteWalkthrough(id: string): Promise<void> {
  const res = await apiV2.DELETE("/api/walkthroughs/{wid}/", {
    params: { path: { wid: id } },
  });
  ensureOk(res, "Failed to delete walkthrough");
}

// Multipart upload — openapi-fetch's bodySerializer is used to pass FormData
// directly rather than JSON-encoding the body.
// Note: openapi-fetch types the body as the schema type; we override with
// `unknown` cast + bodySerializer to send raw FormData for multipart.
export async function uploadWalkthrough(
  form: FormData,
): Promise<WalkthroughDetail> {
  const res = await apiV2.POST("/api/walkthroughs/", {
    // openapi-fetch defaults to JSON; override for multipart so the browser
    // sets the Content-Type boundary automatically.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    body: form as any,
    bodySerializer: (body: unknown) => body as FormData,
  });
  const data = unwrap(res, "Upload failed");
  return data as unknown as WalkthroughDetail;
}

export function walkthroughContentUrl(
  workspace: string,
  id: string,
  token?: string | null,
): string {
  // The bytes live under the walkthrough's workspace, like its page — the only
  // address they have (canopy-web#1337). Base-aware so the `<video>`/`<iframe>`
  // src resolves under a deployed sub-path, not the origin root. Anonymous
  // public access carries the share token as ?t=.
  const suffix = token ? `?t=${encodeURIComponent(token)}` : "";
  return withBase(scopedPath(workspace, `/walkthrough/${id}/content${suffix}`));
}

export async function rotateWalkthroughToken(
  id: string,
): Promise<WalkthroughDetail> {
  const res = await apiV2.POST("/api/walkthroughs/{wid}/rotate-token", {
    params: { path: { wid: id } },
  });
  const data = unwrap(res, "Failed to rotate share link");
  return data as unknown as WalkthroughDetail;
}
