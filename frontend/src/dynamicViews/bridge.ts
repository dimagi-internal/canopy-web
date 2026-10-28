/**
 * What a dynamic view's frame may make the canopy page read — the one gate.
 *
 * The frame has no network; every read it wants arrives here as a `ReadRequest`
 * and leaves (if at all) as a GET to `/api/w/<the view's workspace>/<catalog path>`,
 * made with the viewer's own session. `resolveRead` is the rule, kept pure so it
 * is tested directly (bridge.test.ts):
 *
 * - the model must be in the catalog the server publishes
 *   (`GET /api/dynamic-views/guide/`, apps/dynamic_views/catalog.py);
 * - only that model's listed query parameters, with scalar values;
 * - path parameters must be the model's own, each a plain identifier, and
 *   `{workspace}` is ALWAYS the view's workspace — never the frame's to choose;
 * - on a project view a missing `handle` is the view's own project.
 *
 * A request that breaks a rule is refused with the reason; nothing is narrowed or
 * guessed. What comes back is whatever the route's own gate lets the viewer read.
 */
import { scopedPath } from '@/lib/scopedLinks'
import type { ReadRequest } from './protocol'

export interface CatalogModel {
  name: string
  path: string
  description: string
  params: string[]
  path_params: string[]
}

export interface ReadContext {
  workspace: string
  project: string | null
}

export const PATH_PARAM_RE = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$/

export type Resolved = { ok: true; url: string } | { ok: false; error: string }

function scalar(v: unknown): string | null {
  if (typeof v === 'string') return v
  if (typeof v === 'number' && Number.isFinite(v)) return String(v)
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  if (Array.isArray(v) && v.every((x) => typeof x === 'string' || typeof x === 'number')) return v.join(',')
  return null
}

export function resolveRead(catalog: CatalogModel[], ctx: ReadContext, req: ReadRequest): Resolved {
  const model = catalog.find((m) => m.name === req?.model)
  if (!model) return { ok: false, error: `unknown model '${String(req?.model)}'` }

  const query = new URLSearchParams()
  const params = req.params ?? {}
  if (typeof params !== 'object' || Array.isArray(params)) return { ok: false, error: 'params must be an object' }
  for (const [k, v] of Object.entries(params)) {
    if (!model.params.includes(k)) return { ok: false, error: `${model.name} does not take '${k}'` }
    if (v == null) continue
    const s = scalar(v)
    if (s === null) return { ok: false, error: `${model.name}: '${k}' must be a string, number or boolean` }
    query.set(k, s)
  }

  const given = req.path ?? {}
  if (typeof given !== 'object' || Array.isArray(given)) return { ok: false, error: 'path must be an object' }
  for (const k of Object.keys(given)) {
    if (!model.path_params.includes(k)) return { ok: false, error: `${model.name} has no path parameter '${k}'` }
  }
  let path = model.path.replace('{workspace}', encodeURIComponent(ctx.workspace))
  for (const p of model.path_params) {
    const raw = given[p] ?? (p === 'handle' ? ctx.project : undefined)
    const v = raw == null ? '' : String(raw)
    if (!v) return { ok: false, error: `${model.name} needs path.${p}` }
    if (!PATH_PARAM_RE.test(v)) return { ok: false, error: `${model.name}: path.${p} is not a valid identifier` }
    path = path.replace(`{${p}}`, encodeURIComponent(v))
  }
  const qs = query.toString()
  return { ok: true, url: `/api/w/${encodeURIComponent(ctx.workspace)}${path}${qs ? `?${qs}` : ''}` }
}

/** Where a view's navigate() lands: a canopy page, under the view's workspace
 * unless it names one; or an outside https or mailto: link, which the page confirms
 * first (a mailto: only opens the viewer's own mail app; nothing is sent for them). */
export function resolveNavigation(workspace: string, path: string):
  { kind: 'app'; to: string } | { kind: 'external'; url: string } | { kind: 'refused' } {
  if (typeof path !== 'string' || !path) return { kind: 'refused' }
  if (/^mailto:[^\s]+$/i.test(path)) return { kind: 'external', url: path }
  if (/^https?:\/\//i.test(path)) {
    try {
      const u = new URL(path)
      return u.protocol === 'https:' ? { kind: 'external', url: u.toString() } : { kind: 'refused' }
    } catch {
      return { kind: 'refused' }
    }
  }
  if (!path.startsWith('/') || path.startsWith('//') || path.startsWith('/api/')) return { kind: 'refused' }
  if (path.startsWith('/w/')) return { kind: 'app', to: path }
  return { kind: 'app', to: scopedPath(workspace, path) }
}
