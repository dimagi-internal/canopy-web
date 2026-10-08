/**
 * API client for the /api/ddd surface (DDD run views).
 *
 * These endpoints aren't in the generated OpenAPI types yet, so we use raw
 * fetch with the same credential conventions as api/reviews.ts. Run
 * `npm run gen:api` after the backend lands to migrate to openapi-fetch.
 */

import { apiUrl, getCsrfToken } from './base'

export type WalkthroughKind = 'html' | 'video'
export type DddLinkKind = 'narrative' | 'companion' | 'reference'

export interface DddNarrativeListItem {
  slug: string
  title: string | null
  phase: string | null
  project_slug: string | null
  run_count: number
  latest_at: string | null
  has_video: boolean
  has_deck: boolean
  has_narrative: boolean
}

export interface DddNarrativeRun {
  run_id: string
  created_at: string | null
  latest_at: string | null
  status: string | null
  gate: string | null
  scene_count: number
  has_video: boolean
  has_deck: boolean
}

export interface DddNarrativeStory {
  review_id: string | null
  version: number | null
  title: string | null
  story: string | null
  video_url: string | null
  video_viewer_url: string | null
}

/** One recorded-narrative cut and its pinned video (canopy-web#1288). */
export interface DddNarrativeCut {
  cut_id: string
  title: string
  scene_ids: string[]
  walkthrough_id: string
  video_url: string
  video_viewer_url: string
  duration_sec?: number | null
}

export interface DddNarrativeVersion {
  version: number | null
  review_id: string | null
  title: string | null
  story: string | null
  // Per-scene narration for this version — carried so the client can render a
  // plain-language before/after (vN -> vN+1) diff without a second fetch.
  narration: DddNarration[]
  created_at: string | null
  gate: string | null
  status: string | null
  video_url: string | null
  video_viewer_url: string | null
  /** A recorded narrative's cuts, in narration order; empty otherwise. */
  cuts?: DddNarrativeCut[]
  runs: DddNarrativeRun[]
}

export interface DddNarrativeDetail {
  slug: string
  title: string | null
  story: string | null
  phase: string | null
  project_slug: string | null
  visibility: 'public' | 'private' | 'mixed'
  current_version: DddNarrativeStory | null
  versions: DddNarrativeVersion[]
}

export interface DddRunArtifact {
  id: string
  title: string
  kind: WalkthroughKind
  role: string | null
  content_url: string
  viewer_url: string
  duration_sec: number | null
}

export interface DddRunArtifactRef {
  id: string
  title: string
  kind: WalkthroughKind
  role: string | null
  created_at: string
  viewer_url: string
}

export interface DddNarration {
  scene?: number
  id?: string
  title?: string
  persona?: string
  provenance?: string
  text: string
  // features and other fields may be present; kept open.
  [k: string]: unknown
}

export interface DddRunNarrative {
  review_id: string | null
  version: number | null
  run_id: string
  gate: string
  title: string | null
  story: string | null
  narration: DddNarration[]
  personas: Record<string, { name?: string; role?: string; color?: string; org?: string }>
  why_brief: Record<string, unknown> | null
}

export interface DddLink {
  label: string
  url: string
  kind: DddLinkKind
}

export interface DddRunPackage {
  run_id: string
  narrative_slug: string
  created_at: string | null
  latest_at: string | null
  phase: string | null
  video: DddRunArtifact | null
  // First-class run outputs. `slides` = the canopy:walkthrough HTML slideshow
  // (role=deck); `documentation` = the feature docs page (role=docs).
  slides: DddRunArtifact | null
  documentation: DddRunArtifact | null
  narrative: DddRunNarrative | null
  links: DddLink[]
  all_artifacts: DddRunArtifactRef[]
}

/** The curated, shareable run-release payload (the clean public-capable page). */
export interface DddRunRelease {
  run_id: string
  narrative_slug: string
  title: string | null
  lede: string | null
  video: DddRunArtifact | null
  documentation: DddRunArtifact | null
  narrative: DddRunNarrative | null
  product_links: DddLink[]
  related_links: DddLink[]
  is_public: boolean
  is_member: boolean
  share_token: string | null
  build_url: string | null
}

async function getJson<T>(url: string): Promise<T> {
  const resp = await fetch(apiUrl(url), { credentials: 'same-origin' })
  if (!resp.ok) {
    let detail = ''
    try {
      detail = (await resp.json())?.detail ?? ''
    } catch {
      /* ignore */
    }
    throw new Error(detail || `Request failed (${resp.status})`)
  }
  return resp.json() as Promise<T>
}

async function del(url: string): Promise<void> {
  const csrf = getCsrfToken()
  const resp = await fetch(apiUrl(url), {
    method: 'DELETE',
    credentials: 'same-origin',
    headers: { ...(csrf ? { 'X-CSRFToken': csrf } : {}) },
  })
  if (!resp.ok) {
    let detail = ''
    try {
      const body = await resp.json()
      detail = body?.detail ?? body?.title ?? ''
    } catch {
      /* ignore */
    }
    throw new Error(detail || `Delete failed (${resp.status})`)
  }
}

async function patchJson<T>(url: string, body: unknown): Promise<T> {
  const csrf = getCsrfToken()
  const resp = await fetch(apiUrl(url), {
    method: 'PATCH',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      ...(csrf ? { 'X-CSRFToken': csrf } : {}),
    },
    body: JSON.stringify(body),
  })
  if (!resp.ok) {
    let detail = ''
    try {
      const b = await resp.json()
      detail = b?.detail ?? b?.title ?? ''
    } catch {
      /* ignore */
    }
    throw new Error(detail || `Request failed (${resp.status})`)
  }
  return resp.json() as Promise<T>
}

export interface ListNarrativesParams {
  project?: string
  mine?: boolean
}

export function listNarratives(
  params: ListNarrativesParams = {},
): Promise<DddNarrativeListItem[]> {
  const q = new URLSearchParams()
  if (params.project) q.set('project', params.project)
  if (params.mine) q.set('mine', 'true')
  const suffix = q.toString() ? `?${q.toString()}` : ''
  return getJson(`/api/ddd/narratives/${suffix}`)
}

export function getNarrative(slug: string): Promise<DddNarrativeDetail> {
  return getJson(`/api/ddd/narratives/${encodeURIComponent(slug)}/`)
}

export interface SetNarrativeVisibilityResult {
  slug: string
  visibility: 'public' | 'private' | 'mixed'
  walkthroughs_updated: number
  reviews_updated: number
}

/** Make an entire narrative public or private (cascades to all artifacts + reviews). */
export function setNarrativeVisibility(
  slug: string,
  makePublic: boolean,
): Promise<SetNarrativeVisibilityResult> {
  return patchJson(`/api/ddd/narratives/${encodeURIComponent(slug)}/visibility/`, {
    visibility: makePublic ? 'link' : 'private',
  })
}

export function getRun(runId: string): Promise<DddRunPackage> {
  return getJson(`/api/ddd/runs/${encodeURIComponent(runId)}/`)
}

/**
 * Fetch the clean, shareable release view of a run. Anonymous-capable: pass the
 * `?t=<share_token>` from the URL so a public viewer (no Dimagi login) is
 * admitted; members are recognised by their session cookie and `token` is
 * unnecessary.
 */
export function getRelease(runId: string, token?: string | null): Promise<DddRunRelease> {
  const q = token ? `?t=${encodeURIComponent(token)}` : ''
  return getJson(`/api/ddd/release/${encodeURIComponent(runId)}/${q}`)
}

/** Delete a single run: its walkthroughs + reviews (best-effort Drive cleanup). */
export function deleteRun(runId: string): Promise<void> {
  return del(`/api/ddd/runs/${encodeURIComponent(runId)}/`)
}

/** Delete one narrative version and the runs nested under it. */
export function deleteNarrativeVersion(slug: string, version: number): Promise<void> {
  return del(`/api/ddd/narratives/${encodeURIComponent(slug)}/versions/${version}/`)
}

/** Delete an entire narrative: every version + run for the slug. */
export function deleteNarrative(slug: string): Promise<void> {
  return del(`/api/ddd/narratives/${encodeURIComponent(slug)}/`)
}
