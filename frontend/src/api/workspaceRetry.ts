// The server's read rule, met by the client (docs/architecture/roles.md,
// "A read that names no workspace stays inside one organisation").
//
// A caller whose workspaces sit in more than one tenant tree (dimagi + a family
// root) gets a 422 `workspace-required` from any FLAT list (`/api/agents/`,
// `/api/harness/turns/`, …): the union would mix organisations. Most pages here
// read flat routes — only a few app prefixes are rewritten onto /api/w/:ws/ on a
// tenant page (client.v2's WS_SCOPED_API_PREFIXES), and pages off a tenant route
// (/supervisor, /sessions, /activity, /schedules) are never rewritten at all.
//
// A GET refused with exactly that problem type is answered from pinned reads:
//
// * on a tenant page (/w/:ws/…) — retried once, pinned to that workspace;
// * on a root page — the fleet views, which span every workspace you belong to
//   by design — LOOPED over the pins the refusal names (`extras.pins`: one per
//   organisation you are in at its top, since a pinned read covers the
//   workspaces below the pin), one pinned read per pin, and the lists merged. Every read is still pinned, so the
//   server never answers a cross-organisation query; the page just asks each
//   workspace in turn (owner decision, Jonathan, 2026-10-11: "everything is
//   properly tied to workspaces, we just loop over them").
//
// It used to retry a root page pinned to ONE guessed default (the first
// membership), which quietly hid every other workspace's agents on /supervisor.
// Merging is a concatenation in workspace order: a root page that shows a
// time-ordered list sorts it itself. Only requests that would otherwise fail are
// touched, so single-tree callers see exactly what they did before.
import { API_BASE } from './base'

export const WORKSPACE_REQUIRED_TYPE_SUFFIX = '/workspace-required'

function workspaceFromPath(pathname: string): string | null {
  const m = pathname.slice(API_BASE.length).match(/^\/w\/([^/]+)(?:\/|$)/)
  return m ? m[1] : null
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
  return (await refusedWorkspaces(res)) !== null
}

/** The workspaces a `workspace-required` refusal names (the caller's own), or
 *  null when the response is not that refusal. */
async function refusedWorkspaces(res: Response): Promise<string[] | null> {
  if (res.status !== 422) return null
  try {
    const body = (await res.clone().json()) as {
      type?: unknown
      extras?: { workspaces?: unknown; pins?: unknown }
    }
    if (typeof body?.type !== 'string' || !body.type.endsWith(WORKSPACE_REQUIRED_TYPE_SUFFIX)) return null
    // The problem body carries its fields under `extras` (apps/api's RFC 7807
    // handler), not at the top level — reading `body.workspaces` found nothing,
    // so a root page looped over zero workspaces and showed the raw refusal.
    // `pins` (2026-10-12): a pinned read covers the workspaces below the pin, so
    // the server names the fewest pins that cover every membership — usually one
    // per organisation. Looping over every workspace instead would read connect
    // twice (once under dimagi) and double the merged counts.
    const named = Array.isArray(body.extras?.pins) ? body.extras.pins : body.extras?.workspaces
    return Array.isArray(named) ? named.filter((s): s is string => typeof s === 'string') : []
  } catch {
    return null
  }
}

type Page = { items: unknown[]; count?: unknown } & Record<string, unknown>

function isPage(v: unknown): v is Page {
  return typeof v === 'object' && v !== null && !Array.isArray(v) && Array.isArray((v as Page).items)
}

function dedupe(rows: unknown[]): unknown[] {
  const seen = new Set<unknown>()
  return rows.filter((r) => {
    const id = typeof r === 'object' && r !== null ? (r as { id?: unknown }).id : undefined
    if (id === undefined || id === null) return true
    if (seen.has(id)) return false
    seen.add(id)
    return true
  })
}

/**
 * One list from several workspaces' answers to the same pinned read, or null
 * when they are not lists this can merge: JSON arrays concatenate; Ninja pages
 * (`{items, count}`) concatenate their items and add their counts. Rows are
 * de-duplicated by `id`. Pure, so it is unit-testable.
 */
export function mergeLists(bodies: unknown[]): unknown | null {
  if (bodies.length === 0) return null
  if (bodies.every(Array.isArray)) return dedupe((bodies as unknown[][]).flat())
  if (bodies.every(isPage)) {
    const pages = bodies as Page[]
    const items = dedupe(pages.flatMap((p) => p.items))
    const counts = pages.map((p) => p.count)
    const merged: Page = { ...pages[0], items }
    if (counts.every((c) => typeof c === 'number')) merged.count = (counts as number[]).reduce((a, b) => a + b, 0)
    return merged
  }
  return null
}

/** Wrap a fetch so a refused flat GET is answered from pinned reads. */
export function withWorkspaceRetry(base: typeof fetch): typeof fetch {
  return async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const res = await base(input, init)
    if (res.status !== 422) return res
    // Read method and URL without constructing a Request: the original has
    // been sent, and re-wrapping one that carried a body throws.
    const isReq = typeof Request !== 'undefined' && input instanceof Request
    const method = (init?.method ?? (isReq ? (input as Request).method : 'GET')).toUpperCase()
    if (method !== 'GET' && method !== 'HEAD') return res
    const named = await refusedWorkspaces(res)
    if (named === null) return res
    const url = isReq ? (input as Request).url : input instanceof URL ? input.toString() : String(input)
    // A GET carries no body, so re-using the original's settings is safe (see
    // client.v2's rewriteForWorkspace for why a body would not be).
    const send = (target: string) => (isReq ? base(new Request(target, input as Request)) : base(target, init))

    const here = workspaceFromPath(window.location.pathname)
    if (here) {
      const target = pinnedUrl(url, here, window.location.origin)
      return target ? send(target) : res
    }

    const targets = named
      .map((ws) => pinnedUrl(url, ws, window.location.origin))
      .filter((t): t is string => t !== null)
    if (targets.length === 0) return res
    const answers = await Promise.all(targets.map(send))
    // A workspace that refuses you this list (403/404: a role gate) holds
    // nothing you may see there; any other failure is the page's to show.
    const failed = answers.find((r) => !r.ok && r.status !== 403 && r.status !== 404)
    if (failed) return failed
    const ok = answers.filter((r) => r.ok)
    if (ok.length === 0) return answers[0]
    let merged: unknown | null
    try {
      merged = mergeLists(await Promise.all(ok.map((r) => r.json())))
    } catch {
      merged = null
    }
    // Not a list (nothing by-id is refused, so this should not happen): show
    // the refusal rather than one workspace's answer passed off as all of them.
    if (merged === null) return res
    return new Response(JSON.stringify(merged), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
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
