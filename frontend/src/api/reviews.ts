/**
 * API client for the /api/reviews/ surface.
 *
 * The reviews endpoints are not yet in the generated OpenAPI types, so we use
 * raw fetch with the same CSRF + credential conventions as client.v2.ts.
 *
 * Auth strategy mirrors the backend contract:
 *   - Authenticated session users: no extra params required.
 *   - Link-token holders (?t=<share_token>): pass token as query param so the
 *     backend's _token_ok() check succeeds for unauthenticated callers.
 */

import { getCsrfToken } from './base'

// ---------------------------------------------------------------------------
// Types (mirrors apps/reviews/schemas.py)
// ---------------------------------------------------------------------------

export type ReviewStatus = 'pending' | 'resolved'
export type ReviewVisibility = 'private' | 'link'

export interface ReviewDecision {
  id: string
  prompt: string
  options: string[]
  recommended: string
  class: string
}

export interface ReviewFeature {
  id: string
  description: string
  verify: string
}

export interface ReviewNarrationItem {
  scene: number
  id: string
  /** Story-beat title (DDD v3). Falls back to "Scene N" when absent. */
  title?: string
  /** Persona key on screen for this beat (DDD v3). Maps into request_json.personas. */
  persona?: string
  /** Spine id this beat grounds — joins the scene to its why-brief grounding. */
  provenance?: string
  text: string
  features?: ReviewFeature[]
}

export interface ReviewPersona {
  name: string
  role: string
  color: string
  intro: string
  /** The organization this individual belongs to (e.g. "Dimagi", "LLO"). */
  org?: string
}

// Why-brief (the grounding doc), shown + edited alongside the narrative.
export interface ReviewWhyEvidence {
  kind?: string
  ref: string
}

export interface ReviewWhySpineItem {
  id: string
  claim: string
  rationale?: string
  status?: string
  evidence?: ReviewWhyEvidence[]
}

export interface ReviewWhyGap {
  id: string
  type?: string
  claim_ref?: string
  detail: string
  proposed_action: string
}

export interface ReviewWhyBrief {
  problem?: string
  spine?: ReviewWhySpineItem[]
  gaps?: ReviewWhyGap[]
}

export interface ReviewVideo {
  walkthrough_id?: string
  url?: string
}

export interface ReviewSceneActionability {
  score: number
  missed: string[]
}

export interface ReviewActionability {
  overall_score: number
  per_scene: Record<string, ReviewSceneActionability>
}

export interface ReviewRequestJson {
  schema_version: number
  run_id: string
  gate: string
  video?: ReviewVideo
  decisions: ReviewDecision[]
  narration: ReviewNarrationItem[]
  /** The cohesive demo narrative — the whole story the scenes decompose (DDD v3). */
  narrative?: string
  /** Persona key -> details, so the surface can show who is on screen each beat. */
  personas?: Record<string, ReviewPersona>
  /** The resolved why-brief (problem/spine/gaps), shown + edited on the surface. */
  why_brief?: ReviewWhyBrief
  autonomous_audit: string[]
  actionability?: ReviewActionability | null
  /**
   * Optional pre-set build order — a sequence of scene ids indicating the
   * tackle order the reviewer should start from. When absent, the editor
   * defaults to the narration order.
   */
  build_order?: string[] | null
}

// ---------------------------------------------------------------------------
// Product-findings reviews (gate === 'product_findings')
//
// A run-child review (NOT a narrative version): the backend forces
// narrative_slug=None / version=0. Its request_json carries an embeddable
// iteration clip + a list of finding clusters; the human resolves it with a
// per-cluster implement/skip/defer decision plus an overall proceed/discuss.
// See CONTRACT-product-findings-review.md.
// ---------------------------------------------------------------------------

export interface FindingsVideo {
  url: string
}

export interface FindingsSummary {
  concept_score?: number
  user_score?: number
  verdict?: string
}

export interface FindingsEvidence {
  scene: number
  /** base64 data-URI JPEG (~480px wide) of the scene snapshot. */
  thumb: string
  /** Deck anchor fragment, e.g. "#scene-9". */
  deck_anchor: string
  /** Integer seconds into the iteration clip (scene start_seconds). */
  video_t: number
}

export type FindingsSeverity = 'high' | 'medium' | 'low'
export type FindingsFixKind = 'mechanical' | 'options' | 'redesign'

export interface FindingsCluster {
  id: string
  title: string
  severity?: FindingsSeverity
  fix_kind?: FindingsFixKind
  route?: string
  scenes?: number[]
  suggested_fix?: string
  count?: number
  evidence?: FindingsEvidence[]
}

export interface ProductFindingsRequestJson {
  run_id: string
  gate: 'product_findings'
  feature?: string
  iteration?: number
  video?: FindingsVideo
  deck_url?: string
  summary?: FindingsSummary
  clusters: FindingsCluster[]
}

export type FindingsDecision = 'implement' | 'skip'

/** Per-finding resolution: a decision (null = commented but not explicitly picked)
 *  + an optional reviewer comment. */
export interface FindingsResolution {
  decision: FindingsDecision | null
  comment: string
}

export interface ProductFindingsResponseJson {
  /** Keyed by cluster id. Only findings the reviewer acted on are present. */
  decisions: Record<string, FindingsResolution>
}

// ---------------------------------------------------------------------------
// Submit payload — new "edited_scenes" contract
// ---------------------------------------------------------------------------

export interface ReviewSubmittedFeature {
  id: string
  description: string
  verify: string
  feedback: string
}

export interface ReviewSubmittedScene {
  id: string
  title: string
  narration: string
  deleted: boolean
  features: ReviewSubmittedFeature[]
  feedback: string
}

/** Partial persona edit — only changed fields are sent, keyed by persona key. */
export type ReviewSubmittedPersonas = Record<string, Partial<Omit<ReviewPersona, 'color'>>>

/** Why-brief edits — prose fields only, keyed by spine/gap id. */
export interface ReviewSubmittedWhyBrief {
  problem?: string
  spine?: Record<string, { claim?: string; rationale?: string }>
  gaps?: Record<string, { detail?: string; proposed_action?: string }>
}

export interface ReviewSubmitPayload {
  decisions: Record<string, string>
  /** Legacy field — kept for backwards-compat read; send is now edited_scenes */
  narration_edits?: Record<string, string>
  edited_scenes?: ReviewSubmittedScene[]
  edited_personas?: ReviewSubmittedPersonas
  edited_why_brief?: ReviewSubmittedWhyBrief
  overall_feedback?: string
  /**
   * The reviewer's chosen build sequence — an ordered list of scene ids
   * indicating the order they intend to tackle the scenes when building.
   * Independent of the video/narration order.
   */
  build_order?: string[]
}

/** A suggestion from an external (share-token) reviewer who cannot resolve the gate.
 *  Only present for a caller who can write (the owner / a workspace member). */
export interface ReviewSuggestion {
  response_json: ReviewSubmitPayload
  name: string | null
  created_at: string
}

export interface ReviewDetail {
  id: string
  run_id: string
  /** Narrative slug this review belongs to (for highlighting in the DDD shell).
   *  null for a run-child gate, which belongs to no narrative — it gets no DDD chrome. */
  narrative_slug: string | null
  gate: string
  status: ReviewStatus
  visibility: ReviewVisibility
  request_json: ReviewRequestJson
  response_json: ReviewSubmitPayload | null
  /** External-reviewer suggestions — empty for anonymous readers; populated for the owner. */
  suggestions: ReviewSuggestion[]
  share_token: string | null
  is_owner: boolean
  /** Whether THIS caller may resolve the gate (an editor of the review's workspace).
   *  Everyone else gets the suggest-only editor. */
  can_decide?: boolean
  /** The narrative's human name — never the run id or a clipped first sentence. */
  title?: string | null
  created_at: string
  resolved_at: string | null
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// "" at root, "/canopy" as a labs tenant — these raw fetches (not openapi-fetch)
// must carry the deployment path prefix themselves.
const API_BASE = import.meta.env.BASE_URL.replace(/\/$/, '')

function reviewUrl(id: string, token?: string | null): string {
  const t = token ? `?t=${encodeURIComponent(token)}` : ''
  return `${API_BASE}/api/reviews/${id}/${t}`
}

function submitUrl(id: string, token?: string | null): string {
  const t = token ? `?t=${encodeURIComponent(token)}` : ''
  return `${API_BASE}/api/reviews/${id}/submit/${t}`
}

function suggestUrl(id: string, token?: string | null): string {
  const t = token ? `?t=${encodeURIComponent(token)}` : ''
  return `${API_BASE}/api/reviews/${id}/suggest/${t}`
}

async function parseResponse<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let msg = `Request failed: ${resp.status}`
    try {
      const body = await resp.json()
      if (body?.detail) msg = body.detail
      else if (body?.title) msg = body.title
    } catch {
      // ignore parse failure
    }
    throw new Error(msg)
  }
  return resp.json() as Promise<T>
}

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------

/** Fetch a review by id. Pass token for link-visibility access. */
export async function getReview(id: string, token?: string | null): Promise<ReviewDetail> {
  const resp = await fetch(reviewUrl(id, token), {
    credentials: 'same-origin',
  })
  return parseResponse<ReviewDetail>(resp)
}

/** Submit decisions + narration edits for a review. */
export async function submitReview(
  id: string,
  payload: ReviewSubmitPayload,
  token?: string | null,
): Promise<ReviewDetail> {
  const csrf = getCsrfToken()
  const resp = await fetch(submitUrl(id, token), {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      ...(csrf ? { 'X-CSRFToken': csrf } : {}),
    },
    body: JSON.stringify({ response_json: payload }),
  })
  return parseResponse<ReviewDetail>(resp)
}

/**
 * Submit a SUGGESTION — it never resolves the gate; the edits land as a suggestion
 * for the review's owner, who is notified. Either an external reviewer with the
 * share token, or a workspace member saving edits without deciding (session +
 * CSRF, no token). Returns the new suggestion count.
 */
export async function suggestReview(
  id: string,
  payload: ReviewSubmitPayload,
  token?: string | null,
  name?: string | null,
  /** A guest's address, so they're cc'd on the notification (ignored when signed in). */
  email?: string | null,
): Promise<{ ok: boolean; suggestion_count: number }> {
  const csrf = getCsrfToken()
  const resp = await fetch(suggestUrl(id, token), {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(csrf ? { 'X-CSRFToken': csrf } : {}) },
    body: JSON.stringify({ response_json: payload, name: name ?? null, email: email ?? null }),
  })
  return parseResponse<{ ok: boolean; suggestion_count: number }>(resp)
}

/**
 * Submit a product-findings review resolution. Reuses the SAME endpoint as
 * submitReview — the backend stores response_json verbatim — but carries the
 * product-findings response shape (per-cluster decisions + overall + notes).
 */
export async function submitFindingsReview(
  id: string,
  payload: ProductFindingsResponseJson,
  token?: string | null,
): Promise<ReviewDetail> {
  const csrf = getCsrfToken()
  const resp = await fetch(submitUrl(id, token), {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      ...(csrf ? { 'X-CSRFToken': csrf } : {}),
    },
    body: JSON.stringify({ response_json: payload }),
  })
  return parseResponse<ReviewDetail>(resp)
}

// ---------------------------------------------------------------------------
// Dashboard: list + delete
// ---------------------------------------------------------------------------

/** One row in the DDD-plans dashboard (mirrors apps/reviews/schemas.ReviewListItemOut). */
export interface ReviewListItem {
  id: string
  run_id: string
  gate: string
  status: ReviewStatus
  visibility: ReviewVisibility
  /** null for a run-child gate (see ReviewDetail.narrative_slug). */
  narrative_slug: string | null
  title: string | null
  scene_count: number
  created_at: string
  resolved_at: string | null
  last_activity_at: string
  share_token: string | null
  is_owner: boolean
}

export type ReviewListOrder =
  | '-last_activity'
  | 'last_activity'
  | '-created'
  | 'created'
  | 'narrative_slug'

export interface ListReviewsParams {
  q?: string
  status?: ReviewStatus
  order?: ReviewListOrder
}

/** List all DDD review requests (plans). Authenticated team users only. */
export async function listReviews(params: ListReviewsParams = {}): Promise<ReviewListItem[]> {
  const qs = new URLSearchParams()
  if (params.q) qs.set('q', params.q)
  if (params.status) qs.set('status', params.status)
  if (params.order) qs.set('order', params.order)
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  const resp = await fetch(`${API_BASE}/api/reviews/${suffix}`, { credentials: 'same-origin' })
  return parseResponse<ReviewListItem[]>(resp)
}

/** Delete a review request (dashboard cleanup). */
export async function deleteReview(id: string): Promise<void> {
  const csrf = getCsrfToken()
  const resp = await fetch(`${API_BASE}/api/reviews/${id}/`, {
    method: 'DELETE',
    credentials: 'same-origin',
    headers: { ...(csrf ? { 'X-CSRFToken': csrf } : {}) },
  })
  if (!resp.ok) {
    let msg = `Delete failed: ${resp.status}`
    try {
      const body = await resp.json()
      if (body?.detail) msg = body.detail
      else if (body?.title) msg = body.title
    } catch {
      // ignore
    }
    throw new Error(msg)
  }
}
