// The server's read rule, met by the client (docs/architecture/roles.md,
// "A read that names no workspace stays inside one organisation").
//
// A caller whose workspaces sit in more than one tenant tree (dimagi + a family
// root) gets a 422 `workspace-required` from any FLAT list (`/api/agents/`,
// `/api/harness/turns/`, …): the union would mix organisations. Most pages here
// read flat routes — only a few app prefixes are rewritten onto /api/w/:ws/ on a
// tenant page (client.v2's WS_SCOPED_API_PREFIXES), and pages off a tenant route
// (/supervisor, /sessions) are never rewritten at all.
//
// Rather than pin every call up front — which would also narrow by-id reads
// (opening a chat in another of your workspaces) and change what single-tree
// users see — a GET refused with exactly that problem type is retried once,
// pinned to the active workspace: the URL's /w/:ws/, else the caller's default
// (the first entry of GET /api/workspaces/, the same default `/` lands on). Only
// requests that would otherwise fail are touched.
import { API_BASE, apiUrl } from './base'

export const WORKSPACE_REQUIRED_TYPE_SUFFIX = '/workspace-required'

let defaultWorkspace: string | null = null
let defaultLookup: Promise<string | null> | null = null

/** WorkspaceProvider reports the active workspace here once it knows it. */
export function setDefaultWorkspace(slug: string | null): void {
  defaultWorkspace = slug
}

function workspaceFromPath(pathname: string): string | null {
  const m = pathname.slice(API_BASE.length).match(/^\/w\/([^/]+)(?:\/|$)/)
  return m ? m[1] : null
}

async function lookupDefault(base: typeof fetch): Promise<string | null> {
  if (defaultWorkspace) return defaultWorkspace
  defaultLookup ??= base(apiUrl('/api/workspaces/'), { credentials: 'same-origin' })
    .then(async (r) => {
      if (!r.ok) return null
      const rows = (await r.json()) as Array<{ slug?: string }>
      return rows[0]?.slug ?? null
    })
    .catch(() => null)
    .finally(() => {
      defaultLookup = null
    })
  return defaultLookup
}

/**
 * The pinned twin of a flat API URL, or null when it is not one this retry may
 * pin: only `/api/…` under this deployment, never one already under `/api/w/`.
 * Pure, so it is unit-testable without a DOM (see workspaceRetry.test.ts).
 */
export function pinnedUrl(url: string, ws: string, origin: string, apiBase = API_BASE): string | null {
  const u = new URL(url, origin)
  if (u.origin !== new URL(origin).origin) return null
  const rel = u.pathname.slice(apiBase.length)
  if (!u.pathname.startsWith(apiBase) || !rel.startsWith('/api/') || rel.startsWith('/api/w/')) return null
  u.pathname = `${apiBase}/api/w/${encodeURIComponent(ws)}${rel.slice('/api'.length)}`
  return u.toString()
}

/** Is this response the server's "name a workspace" refusal? */
export async function isWorkspaceRequired(res: Response): Promise<boolean> {
  if (res.status !== 422) return false
  try {
    const body = (await res.clone().json()) as { type?: unknown }
    return typeof body?.type === 'string' && body.type.endsWith(WORKSPACE_REQUIRED_TYPE_SUFFIX)
  } catch {
    return false
  }
}

/** Wrap a fetch so a refused flat GET is retried once, pinned. */
export function withWorkspaceRetry(base: typeof fetch): typeof fetch {
  return async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const res = await base(input, init)
    if (res.status !== 422) return res
    // Read method and URL without constructing a Request: the original has
    // been sent, and re-wrapping one that carried a body throws.
    const isReq = typeof Request !== 'undefined' && input instanceof Request
    const method = (init?.method ?? (isReq ? (input as Request).method : 'GET')).toUpperCase()
    if (method !== 'GET' && method !== 'HEAD') return res
    if (!(await isWorkspaceRequired(res))) return res
    const ws = workspaceFromPath(window.location.pathname) ?? (await lookupDefault(base))
    if (!ws) return res
    const url = isReq ? (input as Request).url : input instanceof URL ? input.toString() : String(input)
    const target = pinnedUrl(url, ws, window.location.origin)
    if (!target) return res
    // A GET carries no body, so re-using the original's settings is safe (see
    // client.v2's rewriteForWorkspace for why a body would not be).
    return isReq ? base(new Request(target, input as Request)) : base(target, init)
  }
}

let installed = false

/** Patch the page's global fetch once. Every API client here — openapi-fetch
 *  (which calls `globalThis.fetch` late, see client.v2) and the raw-fetch
 *  modules — goes through it. */
export function installWorkspaceRetry(): void {
  if (installed || typeof window === 'undefined') return
  installed = true
  window.fetch = withWorkspaceRetry(window.fetch.bind(window))
}
