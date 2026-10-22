/**
 * API client for the /api/reviews/ surface, on `apiV2`.
 *
 * Auth strategy mirrors the backend contract:
 *   - Authenticated session users: no extra params required.
 *   - Link-token holders (?t=<share_token>): pass token as query param so the
 *     backend's _token_ok() check succeeds for unauthenticated callers. Their
 *     page (/w/<ws>/review/<id>) is a scoped viewer path, so apiV2 neither
 *     rewrites it onto /api/w/<ws>/ nor bounces a 401 to login.
 *
 * The row types alias the generated schemas; `request_json` / `response_json`
 * / `suggestions` are free-form JSON there, so their shapes are written out
 * here (mirroring what the orchestrator posts).
 */

import { apiV2, shareQuery } from './client.v2'
import type { components } from './generated'
import { ensureOk, unwrap } from './problem'

type Schemas = components['schemas']

// ---------------------------------------------------------------------------
// Types (mirrors apps/reviews/schemas.py)
// ---------------------------------------------------------------------------

export type ReviewStatus = Schemas['ReviewRequestOut']['status']
export type ReviewVisibility = Schemas['ReviewRequestOut']['visibility']

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
  // --- Optional demo framing (all additive; rendered when present) ---------
  /** The narrative's human name ("Part 1 — RUTF stock at scale"). */
  title?: string
  /** Who the demo is for ("Edesia and OES"). */
  audience?: string
  /** The one point the demo must land. */
  purpose?: string
  /** What a reader must know about the on-screen data. */
  data_disclosure?: string
  /** The storyboard this demo is part of — the set's one link. */
  storyboard_url?: string
  storyboard_title?: string
  /** Per-cut words, keyed by the recipe's cuts[].id. */
  cuts?: Array<{
    id: string
    title?: string
    screen_notes?: string
    audience_note?: string
    provenance?: string
    poster_url?: string
    duration_sec?: number
  }>
  assumptions?: string[]
  divergences?: Array<{ script: string; data?: string; resolution?: string }>
  open_questions?: string[]
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

/** A video pinned to the review's narrative version (canopy-web#1288). For a
 *  recorded narrative there is one per cut, named by the recipe's cut id;
 *  `video_url` is null when the video is private and the reader is not a member
 *  of its workspace. */
export type ReviewPinnedVideo = Schemas['ReviewPinnedVideoOut']

export type ReviewDetail = Omit<
  Schemas['ReviewRequestOut'],
  'request_json' | 'response_json' | 'suggestions'
> & {
  request_json: ReviewRequestJson
  response_json?: ReviewSubmitPayload | null
  /** External-reviewer suggestions — empty for anonymous readers; populated for the owner. */
  suggestions: ReviewSuggestion[]
}

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------

/** Fetch a review by id. Pass token for link-visibility access. `ws` is the
 *  workspace the page's URL names — a review from another one 404s there. */
export async function getReview(
  id: string,
  token?: string | null,
  ws?: string | null,
): Promise<ReviewDetail> {
  const res = await apiV2.GET('/api/reviews/{rid}/', {
    params: { path: { rid: id }, query: shareQuery(token, { ws }) },
  })
  return unwrap(res, 'getReview') as unknown as ReviewDetail
}

/**
 * Where a review's page now lives: its narrative on a storyboard (app-relative,
 * carrying the board's share token only when `token` is the review's own valid
 * one), or `url: null` for a findings review, whose page stays.
 */
export async function getReviewDestination(
  id: string,
  token?: string | null,
  ws?: string | null,
): Promise<{ url: string | null }> {
  const res = await apiV2.GET('/api/reviews/{rid}/destination', {
    params: { path: { rid: id }, query: shareQuery(token, { ws }) },
  })
  const out = unwrap(res, 'getReviewDestination')
  return { url: out.url ?? null }
}

/** Submit decisions + narration edits for a review. */
export async function submitReview(
  id: string,
  payload: ReviewSubmitPayload,
  token?: string | null,
): Promise<ReviewDetail> {
  const res = await apiV2.POST('/api/reviews/{rid}/submit/', {
    params: { path: { rid: id }, query: shareQuery(token) },
    body: { response_json: payload as unknown as Record<string, unknown> },
  })
  return unwrap(res, 'submitReview') as unknown as ReviewDetail
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
  const res = await apiV2.POST('/api/reviews/{rid}/suggest/', {
    params: { path: { rid: id }, query: shareQuery(token) },
    body: {
      response_json: payload as unknown as Record<string, unknown>,
      name: name ?? null,
      email: email ?? null,
    },
  })
  return unwrap(res, 'suggestReview')
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
  const res = await apiV2.POST('/api/reviews/{rid}/submit/', {
    params: { path: { rid: id }, query: shareQuery(token) },
    body: { response_json: payload as unknown as Record<string, unknown> },
  })
  return unwrap(res, 'submitFindingsReview') as unknown as ReviewDetail
}

// ---------------------------------------------------------------------------
// Dashboard: list + delete
// ---------------------------------------------------------------------------

/** One row in the DDD-plans dashboard. */
export type ReviewListItem = Schemas['ReviewListItemOut']

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
  const res = await apiV2.GET('/api/reviews/', {
    params: {
      query: {
        ...(params.q ? { q: params.q } : {}),
        ...(params.status ? { status: params.status } : {}),
        ...(params.order ? { order: params.order } : {}),
      },
    },
  })
  return unwrap(res, 'listReviews') as unknown as ReviewListItem[]
}

/** Delete a review request (dashboard cleanup). */
export async function deleteReview(id: string): Promise<void> {
  const res = await apiV2.DELETE('/api/reviews/{rid}/', { params: { path: { rid: id } } })
  ensureOk(res, 'Delete failed')
}
