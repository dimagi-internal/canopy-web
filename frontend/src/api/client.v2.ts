import createClient from "openapi-fetch";
import type { paths } from "./generated";
import { API_BASE, getCsrfToken } from "./base";

// A lapsed session should bounce the user through OAuth exactly ONCE. The guard
// below is what makes that a bounce and not a loop: if we land back here still
// 401 within this window of the last bounce, the round-trip isn't sticking (a
// rejected cookie, an IdP hiccup, a service worker shadowing /accounts/ — the
// #244 outage), so we STOP navigating and let the 401 fall through to the
// in-app login screen, which the user drives by hand. Defense in depth: an
// infinite redirect loop is now structurally impossible regardless of *why* the
// session won't take. sessionStorage (not a module-scope var) because the
// timestamp must survive the full-page OAuth navigation to Google and back.
const LOGIN_REDIRECT_AT_KEY = "canopy:auth:lastLoginRedirectAt";
const LOGIN_LOOP_WINDOW_MS = 10_000;

/**
 * Pure loop-breaker decision, split out so it's unit-testable without a DOM
 * (this file's convention — see rewriteForWorkspace). Bounce to OAuth only if we
 * have NOT already bounced within the window; a second 401 inside it means the
 * round-trip isn't sticking, so hold and let the login screen render instead.
 */
export function shouldBounceToLogin(lastRedirectAt: number, now: number): boolean {
  return !(lastRedirectAt > 0 && now - lastRedirectAt < LOGIN_LOOP_WINDOW_MS);
}

/**
 * Clear the loop guard once a request has authenticated, so a genuine later
 * expiry still gets its own clean single bounce. Called by AuthProvider on a
 * successful /api/me. Best-effort — sessionStorage can throw in private mode.
 */
export function noteAuthSucceeded(): void {
  try {
    sessionStorage.removeItem(LOGIN_REDIRECT_AT_KEY);
  } catch {
    /* sessionStorage unavailable (private mode / sandboxed frame) — guard is best-effort */
  }
}

function redirectToLogin(): void {
  let lastRedirectAt = 0;
  try {
    lastRedirectAt = Number(sessionStorage.getItem(LOGIN_REDIRECT_AT_KEY)) || 0;
  } catch {
    /* unavailable — treat as "never redirected" and allow the one bounce */
  }
  if (!shouldBounceToLogin(lastRedirectAt, Date.now())) {
    // We bounced moments ago and we're back with another 401 — we're looping.
    // Do NOT navigate; let the caller surface the login screen (getMe → null →
    // <LoginPrompt/>, whose Sign-in button is a user-driven single attempt).
    return;
  }
  try {
    sessionStorage.setItem(LOGIN_REDIRECT_AT_KEY, String(Date.now()));
  } catch {
    /* unavailable — a best-effort guard is better than blocking the redirect */
  }
  const next = encodeURIComponent(window.location.pathname + window.location.search);
  // Prefix-aware: BASE_URL is "/" at root and "/canopy/" as a labs tenant, so
  // this stays under the deployment instead of bouncing to a sibling tenant.
  window.location.href = `${import.meta.env.BASE_URL.replace(/\/$/, '')}/accounts/google/login/?next=${next}`;
}

// Per-token public-link routes (e.g. /review/<id>?t=…) self-gate on their share
// token, so a 401 from an incidental authenticated call (e.g. /api/me) must NOT
// bounce an anonymous visitor to login. Keep in sync with AuthProvider.
function isPublicLinkRoute(): boolean {
  // Strip the deployment prefix (/canopy) before matching the app route.
  const base = import.meta.env.BASE_URL.replace(/\/$/, "");
  const p = window.location.pathname.slice(base.length);
  return (
    p.startsWith("/review/") ||
    p.startsWith("/walkthrough/") ||
    p.startsWith("/share/") ||
    p.startsWith("/ddd-release/") ||
    p.startsWith("/invite/")
  );
}

// Under a path prefix (e.g. /canopy), API calls must carry it too. API_BASE is
// "" at root and "/canopy" as a labs tenant (see ./base).
export const apiV2 = createClient<paths>({
  baseUrl: API_BASE,
  credentials: "same-origin",
});

// Workspace-scoped apps: when the browser is under a tenant route (/w/:ws/…),
// rewrite their flat /api/<app>/… calls to the canonical tenant path
// /api/w/:ws/<app>/…. The schema documents the flat paths (openapi-fetch is
// typed off them); WorkspaceResolveMiddleware on the server gates membership
// and strips the prefix back to the flat mount. Off a tenant route the flat
// path is used and the server resolves the caller's default workspace.
const WS_SCOPED_API_PREFIXES = [
  "/api/projects",
  "/api/walkthroughs",
  "/api/reviews",
  "/api/shareouts",
  "/api/ddd",
  "/api/timeline",
  "/api/agents",
  // Items are agent-scoped, so /api/agents already covers the collection; the
  // resource routes (/api/items/{id}/decide) are a separate top-level prefix and
  // must be pinned too, or one of the two calls would be tenant-scoped and the
  // other not.
  "/api/items",
];

// A request header a caller sets to pin the tenant explicitly when the page it
// dispatches from is not itself workspace-scoped. Never leaves the browser — the
// onRequest middleware consumes it and rewrites the path to /api/w/:ws/…. Exported
// so callers reference the constant rather than re-typing the string.
export const WORKSPACE_HEADER = "X-Canopy-Workspace";

function activeWorkspaceFromUrl(): string | null {
  // Strip the deployment prefix (/canopy) before reading the app route.
  const p = window.location.pathname.slice(API_BASE.length);
  const m = p.match(/^\/w\/([^/]+)(?:\/|$)/);
  return m ? m[1] : null;
}

/**
 * Rewrite a flat /api/<app>/… request onto the tenant path
 * /api/w/:ws/<app>/…, preserving the body. Exported (rather than inlined in
 * the onRequest closure) so it's unit-testable without a live DOM/fetch
 * environment — see client.v2.test.ts.
 *
 * Do NOT do `new Request(url, request)`: that constructor form adopts the
 * source Request's body as a *live stream*, which consumes/disturbs it — any
 * POST/PATCH/PUT then fails at the network layer with "TypeError: Failed to
 * fetch" (no response, no status, nothing to debug from). Read the body
 * fully first instead; GET/HEAD cannot carry a body at all (the Request
 * constructor throws if one is supplied), so omit it for those methods.
 */
export async function rewriteForWorkspace(request: Request, ws: string): Promise<Request> {
  const url = new URL(request.url);
  // openapi-fetch already prefixed API_BASE; match + rewrite against the
  // deployment-relative path so /canopy/api/projects → /canopy/api/w/:ws/projects.
  const rel = url.pathname.slice(API_BASE.length);
  url.pathname = `${API_BASE}/api/w/${ws}${rel.slice("/api".length)}`;
  const hasBody = !["GET", "HEAD"].includes(request.method);
  const body = hasBody ? await request.arrayBuffer() : undefined;
  return new Request(url, {
    method: request.method,
    headers: request.headers,
    body,
    credentials: request.credentials,
    mode: request.mode,
    cache: request.cache,
    redirect: request.redirect,
    referrer: request.referrer,
    referrerPolicy: request.referrerPolicy,
    integrity: request.integrity,
    keepalive: request.keepalive,
    signal: request.signal,
  });
}

apiV2.use({
  async onRequest({ request }) {
    // Explicit workspace override. A caller on a NON-tenant surface (/supervisor,
    // whose URL carries no /w/:ws/) can still pin a workspace by setting this
    // header — used when a resource is workspace-owned but the page it is dispatched
    // from is not (a repo turn's tenant, chosen in the composer). Takes precedence
    // over the URL, applies to ANY path (not just the WS_SCOPED_API_PREFIXES list,
    // which is for the implicit URL-driven case), and is stripped before the send.
    const pinned = request.headers.get(WORKSPACE_HEADER);
    if (pinned) {
      request.headers.delete(WORKSPACE_HEADER);
      return rewriteForWorkspace(request, pinned);
    }
    const ws = activeWorkspaceFromUrl();
    if (!ws) return request;
    const url = new URL(request.url);
    // openapi-fetch already prefixed API_BASE; match against the
    // deployment-relative path so /canopy/api/projects → /canopy/api/w/:ws/projects.
    const rel = url.pathname.slice(API_BASE.length);
    if (WS_SCOPED_API_PREFIXES.some((p) => rel.startsWith(p))) {
      return rewriteForWorkspace(request, ws);
    }
    return request;
  },
});

// CSRF + 401 handling globally via openapi-fetch middleware.
apiV2.use({
  async onRequest({ request }) {
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method)) {
      const token = getCsrfToken();
      if (token) request.headers.set("X-CSRFToken", token);
    }
    return request;
  },
  async onResponse({ response }) {
    if (response.status === 401 && !isPublicLinkRoute()) {
      redirectToLogin();
    }
    return response;
  },
});
