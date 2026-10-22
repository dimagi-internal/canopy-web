/**
 * Client for the shared-session surface (/api/sessions + /api/share).
 *
 * On `apiV2` like every other module. The public /share/{token} read still
 * works for anonymous visitors: its page (/w/<ws>/share/<token>) is a scoped
 * viewer path, which apiV2 neither rewrites onto /api/w/<ws>/ nor bounces to
 * login on a 401 (see client.v2's isPublicLinkRoute).
 */

import { apiV2 } from "./client.v2";
import type { components } from "./generated";
import { ApiError, ensureOk, unwrap } from "./problem";
import { withBase } from "../lib/basePath";
import { scopedPath } from "../lib/scopedLinks";

type Schemas = components["schemas"];

export type SessionVisibility = Schemas["SessionListItemOut"]["visibility"];
export type MessageRole = Schemas["SessionMessageOut"]["role"];
export type SessionMessage = Schemas["SessionMessageOut"];
export type SessionListItem = Schemas["SessionListItemOut"];
/** One arc section in the public view — a member session's turn-synthesis. */
export type SharedSection = Schemas["SharedSectionOut"];
/**
 * Public read-only payload for /api/share/{token}. Discriminated by `kind`:
 * a single `session` (messages populated) or an `arc` (sections populated).
 * `workspace` is where it is shared from — its page is /w/<workspace>/share/<token>.
 */
export type SharedView = Schemas["SharedViewOut"];

/** The shared `ApiError` (./problem); callers branch on `.code` ("not-found"). */
export { ApiError };

/** Public, read-only — works for anonymous visitors with a valid token.
 * Resolves either a single shared session or a multi-session arc. */
export async function getShared(token: string, ws?: string | null): Promise<SharedView> {
  // `ws` is the workspace the page's URL names: a share from another one 404s.
  const res = await apiV2.GET("/api/share/{token}", {
    params: { path: { token }, ...(ws ? { query: { ws } } : {}) },
  });
  return unwrap(res, "getShared") as unknown as SharedView;
}

export async function listMySessions(): Promise<SessionListItem[]> {
  const res = await apiV2.GET("/api/sessions/");
  return unwrap(res, "listMySessions") as unknown as SessionListItem[];
}

export async function rotateSessionToken(slug: string): Promise<{ share_token: string }> {
  const res = await apiV2.POST("/api/sessions/{slug}/rotate-token", { params: { path: { slug } } });
  return unwrap(res, "rotateSessionToken");
}

export async function setSessionVisibility(
  slug: string,
  visibility: SessionVisibility,
): Promise<SessionListItem> {
  const res = await apiV2.PATCH("/api/sessions/{slug}", {
    params: { path: { slug } },
    body: { visibility },
  });
  return unwrap(res, "setSessionVisibility") as unknown as SessionListItem;
}

export async function deleteSession(slug: string): Promise<void> {
  const res = await apiV2.DELETE("/api/sessions/{slug}", { params: { path: { slug } } });
  ensureOk(res, "deleteSession");
}

/** The page to send — under the workspace it was shared from. A share with no
 * workspace has no address (there is no flat one): null. */
export function shareUrl(token: string, workspace?: string | null): string | null {
  if (!workspace) return null;
  return `${window.location.origin}${withBase(scopedPath(workspace, `/share/${token}`))}`;
}
