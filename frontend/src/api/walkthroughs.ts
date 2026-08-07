import { apiV2 } from "./client.v2";
import type { components } from "./generated";
import { withBase } from "../lib/basePath";
import { problemMessage } from "./problem";

/**
 * A failed walkthrough call, carrying the HTTP status.
 *
 * The status is the whole point: a private walkthrough 404s to an anonymous
 * caller (deliberate existence-hiding), which is indistinguishable from a
 * genuine network failure once it has been flattened into a bare
 * `Error("Failed to load walkthrough")`. The viewer needs the number to offer a
 * sign-in instead of a dead end — canopy-web#516.
 *
 * Same shape as `WorkspaceApiError`; see the note in `workspaces.ts` for why
 * these branch on `res.response.ok` rather than `res.error`.
 */
export class WalkthroughApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

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
  const { data, error } = await apiV2.GET("/api/walkthroughs/", {
    params: {
      query: {
        ...(filters.project ? { project: filters.project } : {}),
        ...(filters.kind ? { kind: filters.kind } : {}),
        ...(filters.mine ? { mine: "true" } : {}),
      },
    },
  });
  if (error) throw new Error("Failed to load walkthroughs");
  return data as unknown as WalkthroughListItem[];
}

export async function getWalkthrough(
  id: string,
  token?: string | null,
): Promise<WalkthroughDetail> {
  const res = await apiV2.GET("/api/walkthroughs/{wid}/", {
    params: {
      path: { wid: id },
      ...(token ? { query: { t: token } } : {}),
    },
  });
  if (!res.response.ok) {
    throw new WalkthroughApiError(
      res.response.status,
      problemMessage(res.error, "Failed to load walkthrough"),
    );
  }
  // openapi-fetch's immutable response type deep-freezes the `links` array
  // (method signatures become `{}`), which no longer structurally matches the
  // plain schema alias. Same cast `listWalkthroughs` uses for its array body.
  return res.data as unknown as WalkthroughDetail;
}

export async function patchWalkthrough(
  id: string,
  patch: PatchWalkthroughInput,
): Promise<WalkthroughDetail> {
  const { data, error } = await apiV2.PATCH("/api/walkthroughs/{wid}/", {
    params: { path: { wid: id } },
    body: {
      title: patch.title ?? null,
      description: patch.description ?? null,
      project_slug: patch.project_slug ?? null,
      visibility: patch.visibility ?? null,
    },
  });
  if (error) throw new Error("Failed to update walkthrough");
  return data as unknown as WalkthroughDetail;
}

export async function deleteWalkthrough(id: string): Promise<void> {
  const { error } = await apiV2.DELETE("/api/walkthroughs/{wid}/", {
    params: { path: { wid: id } },
  });
  if (error) throw new Error("Failed to delete walkthrough");
}

// Multipart upload — openapi-fetch's bodySerializer is used to pass FormData
// directly rather than JSON-encoding the body.
// Note: openapi-fetch types the body as the schema type; we override with
// `unknown` cast + bodySerializer to send raw FormData for multipart.
export async function uploadWalkthrough(
  form: FormData,
): Promise<WalkthroughDetail> {
  const { data, error } = await apiV2.POST("/api/walkthroughs/", {
    // openapi-fetch defaults to JSON; override for multipart so the browser
    // sets the Content-Type boundary automatically.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    body: form as any,
    bodySerializer: (body: unknown) => body as FormData,
  });
  if (error) throw new Error("Upload failed");
  return data as unknown as WalkthroughDetail;
}

export function walkthroughContentUrl(id: string, token?: string | null): string {
  // Base-aware so the `<video>`/`<iframe>` src resolves under the deployed
  // sub-path (e.g. `/canopy/walkthrough/<id>/content`), not the origin root.
  // Anonymous public access carries the share token as ?t=.
  const suffix = token ? `?t=${encodeURIComponent(token)}` : "";
  return withBase(`/walkthrough/${id}/content${suffix}`);
}

export async function rotateWalkthroughToken(
  id: string,
): Promise<WalkthroughDetail> {
  const { data, error } = await apiV2.POST("/api/walkthroughs/{wid}/rotate-token", {
    params: { path: { wid: id } },
  });
  if (error) throw new Error("Failed to rotate share link");
  return data as unknown as WalkthroughDetail;
}
